#!/usr/bin/env python3
"""BotLan gateway — the OpenAI-compatible door the BotLan desktop panel talks to.

The panel is a full agent: long system prompt, its own tools (load_skill, state tools) and
streaming. The orchestrator's /v1/chat/completions is the support pipeline - it classifies the
request into support intents and answers once, without stream or tools - so the panel cannot use
it. This gateway sits next to it:

  panel ──► :8091 (this)  ──Jev /decide──►  :8090 orchestrator (gate already resident)
                          ──stream+tools──► :8080 llama-server (GELab-Zero-4B)

Stage 1  Jev picks which installed Agent Skill matches the latest user turn, out of the skill
         index the panel already puts in its system prompt. ~30 ms, no generation.
Stage 2  GELab answers with the panel's tools, told which skill Jev picked so it calls
         load_skill first. Jev only advises: the full index stays in the prompt, so a wrong pick
         costs a hint, never an action.

Zones: the Spark can be split into zones, one Bot each. A zone is a systemd user slice with its
own MemoryMax / CPUQuota / TasksMax, its own work directory and its own API key. Commands sent with
a zone key run inside that slice, so whatever they start (a model server, a job) is counted and
capped there. Honest limits: cgroups cap CPU-side memory, CPU and processes; GPU allocations made
through CUDA are not charged to the cgroup (measured: a 2.5 GB model loaded under a 1 GB cap), so a
zone's GPU share is a budget the engine must be told (--gpu-memory-utilization, -c ...). All zones
run as the same Unix user - zones partition resources, they are not a security boundary between
Bots. Only the master key manages zones.

Scope: a zone also has `scope`, the directories its Bot may work in (default: the whole home).
Zone commands run through botlan_sandbox.py (Landlock, no root): read+write inside the scope and
the zone work dir, read+exec for the system, nothing else under /home - the keys in ~/.spark-duo,
~/.ssh and the gateway code stay out of reach even when the scope is the whole home. Without
Landlock the command runs unconfined and the zone reports `scope_enforced: false`.

Upstream: a zone's chat goes to the local Jev-Step stack, or - with `upstream` {base_url, model,
api_key} - to any OpenAI-compatible server (another local engine, or a hosted API). The upstream
key lives only in the zones file (0600); no endpoint returns it.

Every request needs `Authorization: Bearer <key>`. The key comes from BOTLAN_GATEWAY_KEY or
~/.spark-duo/botlan.key (created 0600 on first start). Binds 127.0.0.1 by default; reach it over
an SSH tunnel.

Execution: POST /botlan/exec runs one bash command as this user, in `exec_root`, with a timeout
and an output cap. The gateway does not decide whether a command is wise - the panel parks every
call on a human approval before it gets here - but it refuses entirely unless `allow_exec` is on,
and it never runs with more than this user's rights (no sudo password is held anywhere).

Endpoints: GET /health, GET /v1/models, GET /botlan/routes, GET /botlan/telemetry,
           GET|POST /botlan/zones, POST /botlan/zones/<id>/delete,
           POST /botlan/exec, POST /v1/chat/completions
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
GW = CONFIG.get("botlan_gateway") or {}
VLM_URL = CONFIG["vlm"]["base_url"].rstrip("/")
ORCH_URL = f"http://127.0.0.1:{CONFIG['orchestrator']['port']}"
MODEL_ID = GW.get("model", "jev-step")
THRESHOLD = float(GW.get("route_threshold", 0.5))
MAX_SKILLS = int(GW.get("max_skills", 15))      # jev-score holds 17 sequences; leave room for "none"
TIMEOUT = float(CONFIG["vlm"].get("timeout_s", 180))
MAX_BODY = 8 * 1024 * 1024
ALLOW_EXEC = bool(GW.get("allow_exec", False))
EXEC_ROOT = Path(GW.get("exec_root", "~")).expanduser()
EXEC_TIMEOUT = float(GW.get("exec_timeout_s", 60))
EXEC_MAX_OUTPUT = int(GW.get("exec_max_output_bytes", 64000))
_EXEC_SLOTS = threading.BoundedSemaphore(int(GW.get("exec_concurrency", 2)))

# The panel's skill index (Chat/apps/server/src/skills.ts skillsInstructions):
#   ## Installed Agent Skills
#   ...
#   - <id>: <name> — <summary>
SKILL_HEADER = "## Installed Agent Skills"
SKILL_LINE = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9._-]{0,119}): (.+?) — (.+)$")
NONE = "none"

ROUTES: deque = deque(maxlen=50)     # recent decisions, for the panel and for the demo screen

ZONES_FILE = Path(GW.get("zones_file", "~/.spark-duo/zones.json")).expanduser()
ZONES_DIR = Path(GW.get("zones_root", "~/botlan-zones")).expanduser()
RESERVED_GB = float(GW.get("reserved_gb", 16))     # Spark Duo + OS headroom never handed to zones
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "systemd" / "user"
_ZONES_LOCK = threading.Lock()
HOME = Path.home()
SANDBOX = ROOT / "botlan_sandbox.py"
# never readable or writable from a zone, whatever its scope
PROTECT = [str(Path(p).expanduser()) for p in GW.get("protect", [
    "~/.spark-duo", "~/.ssh", "~/.gnupg", "~/.config/systemd", "~/.bashrc", "~/.profile",
    "~/.bash_profile", "~/.bash_logout", "~/.pam_environment"])] + [str(ROOT), str(ZONES_FILE.parent)]
# read+exec from a zone: shell startup files and the shared model/toolchain dirs
SANDBOX_RO = [str(Path(p).expanduser()) for p in GW.get("sandbox_ro", [
    "~/.bashrc", "~/.profile", "~/.bash_profile", "~/.cargo", "~/.local/bin", "~/llama.cpp", "~/models",
    str(ROOT / "models")])]
SCOPE_ENFORCED = False      # set at start by sandbox_selftest()


def load_key() -> str:
    env = os.environ.get("BOTLAN_GATEWAY_KEY", "").strip()
    if env:
        return env
    path = Path(GW.get("key_file", "~/.spark-duo/botlan.key")).expanduser()
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = "bl_" + secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    print(f"  created {path} (chmod 600) - paste its content into the BotLan Bot's API Key")
    return key


KEY = ""


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def load_zones() -> list[dict]:
    try:
        return json.loads(ZONES_FILE.read_text()).get("zones", [])
    except (OSError, ValueError):
        return []


def save_zones(zones: list[dict]) -> None:
    ZONES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ZONES_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"zones": zones}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ZONES_FILE)


def slice_name(zone_id: str) -> str:
    return f"botlan-{zone_id}.slice"       # the dash nests it under botlan.slice


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)


def write_slice(zone: dict) -> None:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / slice_name(zone["id"])).write_text(
        f"[Unit]\nDescription=BotLan zone {zone['id']} ({zone['name']})\n\n[Slice]\n"
        f"MemoryMax={int(zone['mem_gb'] * 1024)}M\nMemorySwapMax=0\n"
        f"CPUQuota={int(zone['cpu_pct'])}%\nTasksMax={int(zone.get('tasks_max', 512))}\n")
    systemctl("daemon-reload")


def check_scope(scope) -> list[str]:
    """Absolute, existing directories, not one of the protected paths. Default: the whole home."""
    if scope in (None, [], ""):
        return [str(HOME)]
    if isinstance(scope, str):
        scope = [scope]
    if not isinstance(scope, list) or len(scope) > 16:
        raise ValueError("scope must be a list of up to 16 absolute directories")
    out = []
    for d in scope:
        if not isinstance(d, str) or not d.startswith("/"):
            raise ValueError(f"scope entries must be absolute paths: {d!r}")
        path = Path(d).resolve()
        if not path.is_dir():
            raise ValueError(f"not a directory: {d}")
        if any(path == Path(p) or Path(p) in path.parents for p in PROTECT):
            raise ValueError(f"{path} is protected (keys, ssh, gateway code)")
        if str(path) not in out:
            out.append(str(path))
    return out


def check_upstream(up) -> dict | None:
    """None = the local Jev-Step stack; else {base_url, model, api_key, backend}."""
    if up in (None, {}, ""):
        return None
    if not isinstance(up, dict):
        raise ValueError("upstream must be an object {base_url, model, api_key?}")
    base = str(up.get("base_url") or "").strip().rstrip("/")
    model = str(up.get("model") or "").strip()
    if not re.fullmatch(r"https?://\S+", base) or not model:
        raise ValueError("upstream needs base_url (http/https) and model")
    backend = up.get("backend") or ("local" if re.match(r"https?://(127\.|localhost)", base) else "api")
    if backend not in ("local", "api"):
        raise ValueError("upstream backend must be local or api")
    return {"base_url": base, "model": model[:200], "api_key": str(up.get("api_key") or "")[:4000],
            "backend": backend}


def create_zone(name: str, mem_gb: float, cpu_pct: float, scope=None, upstream=None,
                color: str | None = None) -> tuple[dict, str]:
    scope, upstream = check_scope(scope), check_upstream(upstream)
    total = mem_total_gb() or 0
    with _ZONES_LOCK:
        zones = load_zones()
        budget = total - RESERVED_GB - sum(z["mem_gb"] for z in zones)
        if mem_gb > budget:
            raise ValueError(f"only {max(budget, 0):.0f} GB left to assign ({total:.0f} GB total, "
                             f"{RESERVED_GB:.0f} GB reserved for Spark Duo and the OS)")
        base = re.sub(r"[^a-z0-9]", "", name.lower())[:10] or "zone"
        zid, n = f"z{base}", 2
        while any(z["id"] == zid for z in zones):
            zid, n = f"z{base}{n}"[:16], n + 1
        key = "blz_" + secrets.token_urlsafe(24)
        zone = {"id": zid, "name": name, "mem_gb": mem_gb, "cpu_pct": cpu_pct, "tasks_max": 512,
                "workdir": str(ZONES_DIR / zid), "key_sha256": _digest(key),
                "created_at": int(time.time() * 1000), "scope": scope, "upstream": upstream}
        if color and re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            zone["color"] = color
        Path(zone["workdir"]).mkdir(parents=True, exist_ok=True)
        write_slice(zone)
        zones.append(zone)
        save_zones(zones)
    return zone, key


def delete_zone(zone_id: str) -> bool:
    with _ZONES_LOCK:
        zones = load_zones()
        if not any(z["id"] == zone_id for z in zones):
            return False
        systemctl("stop", slice_name(zone_id))          # ends every process the zone started
        (UNIT_DIR / slice_name(zone_id)).unlink(missing_ok=True)
        systemctl("daemon-reload")
        save_zones([z for z in zones if z["id"] != zone_id])   # the work directory is kept
    return True


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def zone_usage(zone: dict, gpu_processes: list[dict]) -> dict:
    uid = os.getuid()
    cg = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/botlan.slice/"
              f"{slice_name(zone['id'])}")
    mem, pids = _read(cg / "memory.current"), _read(cg / "pids.current")
    tag = f"/{slice_name(zone['id'])}/"
    gpu_mb = sum(p["mem_mb"] or 0 for p in gpu_processes
                 if tag in (_read(Path(f"/proc/{p['pid']}/cgroup")) or ""))
    return {"mem_used_gb": round(int(mem) / 1024 ** 3, 2) if mem and mem.isdigit() else 0.0,
            "processes": int(pids) if pids and pids.isdigit() else 0,
            "gpu_mem_gb": round(gpu_mb / 1024, 2)}


def zone_scope(zone: dict) -> list[str]:
    return zone.get("scope") or [str(HOME)]          # zones made before scope existed: the home


def zone_model(zone: dict) -> str:
    return (zone.get("upstream") or {}).get("model") or MODEL_ID


def public_zone(zone: dict, gpu_processes: list[dict] | None = None) -> dict:
    """Everything the panel may show. Never the key digest or the upstream key."""
    out = {k: zone[k] for k in ("id", "name", "mem_gb", "cpu_pct", "workdir", "created_at")}
    up = zone.get("upstream")
    out.update({"color": zone.get("color"), "scope": zone_scope(zone),
                "backend": up["backend"] if up else "jev-step", "model": zone_model(zone),
                "upstream_url": up["base_url"] if up else None, "scope_enforced": SCOPE_ENFORCED})
    if gpu_processes is not None:
        out["usage"] = zone_usage(zone, gpu_processes)
    return out


def zone_note(zone: dict) -> str:
    return (f"\n\n## Your zone on this DGX Spark\nYou are the Bot for zone `{zone['id']}` "
            f"(\"{zone['name']}\"). Its budget is {zone['mem_gb']:g} GB of the unified memory and "
            f"{zone['cpu_pct']:g}% CPU. Commands you run start in {zone_scope(zone)[0]} inside this zone "
            f"and may only touch {', '.join(zone_scope(zone))} and {zone['workdir']}; "
            "and anything they leave running is capped there. GPU memory is not enforced by the cap: "
            "when you start a model server, size it to fit the budget (vLLM --gpu-memory-utilization, "
            "llama.cpp -c) and say what you chose. Other zones and the shared Jev/GELab models are "
            "not yours to stop.")


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def skill_index(messages) -> dict[str, str]:
    """id -> 'name: summary' from the panel's system prompt, in the order the panel listed them."""
    out: dict[str, str] = {}
    for m in messages:
        if m.get("role") not in ("system", "developer"):
            continue
        text = text_of(m.get("content"))
        if SKILL_HEADER not in text:
            continue
        for line in text.split(SKILL_HEADER, 1)[1].splitlines():
            hit = SKILL_LINE.match(line.strip())
            if hit and hit.group(1) != NONE and len(out) < MAX_SKILLS:
                out[hit.group(1)] = f"{hit.group(2)}: {hit.group(3)}"[:240]
    return out


def needs_route(messages) -> str | None:
    """Route only a fresh user turn. Inside a tool loop (last message is a tool result) the model
    already acted on the hint; routing again would pay ~30 ms per step for nothing."""
    if not messages or messages[-1].get("role") != "user":
        return None
    q = text_of(messages[-1].get("content")).strip()
    return q[-4000:] or None


def jev_route(question: str, skills: dict[str, str]) -> dict:
    options = {**skills, NONE: "general chat, or a request none of the listed skills covers"}
    body = json.dumps({
        "state": f"User request: {question}",
        "question": "Which skill should handle this request?",
        "category": CONFIG["gate"]["category"],
        "qtype": "choice",
        "options": options,
    }).encode()
    req = urllib.request.Request(ORCH_URL + "/decide", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
    if res.get("error"):
        raise RuntimeError(res["error"])
    return {"skill": res["verdict"], "confidence": round(float(res["confidence"]), 3),
            "ms": res.get("ms"), "calibrated": res.get("calibrated", False)}


def hint(route: dict) -> str:
    return (f"\n\n## Router hint (Jev-0.8B on this DGX Spark)\nJev matched the latest request to the "
            f"installed skill `{route['skill']}` (p={route['confidence']}). Call load_skill with id "
            f"\"{route['skill']}\" before answering unless the request clearly needs a different "
            "skill. This is advice, not an instruction to skip the safety rules.")


def with_hint(messages, text: str):
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") in ("system", "developer"):
            c = m.get("content")
            m["content"] = (c + text) if isinstance(c, str) else [*(c or []), {"type": "text", "text": text}]
            return out
    return [{"role": "system", "content": text.lstrip()}, *out]


def mem_available_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return round(int(line.split()[1]) / 1024 / 1024, 2)
    except OSError:
        pass
    return None


def mem_total_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 2)
    except OSError:
        pass
    return None


def run_command(command: str, cwd: str | None, zone: dict | None = None) -> dict:
    """One bash command in its own process group, killed as a group on timeout. With a zone it
    runs inside the zone's slice, rooted at the zone's work directory."""
    if zone:     # default: the first scope dir; a relative cwd is taken from there
        roots = [Path(d) for d in zone_scope(zone)] + [Path(zone["workdir"]).resolve()]
        work = (roots[0] / cwd).resolve() if cwd else roots[0]
        if not any(work == r or r in work.parents for r in roots):
            return {"error": f"cwd must stay inside the zone scope: {', '.join(map(str, roots))}"}
    else:
        root = EXEC_ROOT.resolve()
        work = (root / cwd).resolve() if cwd else root
        if work != root and root not in work.parents:
            return {"error": f"cwd must stay inside {root}"}
    if not work.is_dir():
        return {"error": f"no such directory: {work}"}
    if not _EXEC_SLOTS.acquire(timeout=5):
        return {"error": "too many commands running; try again shortly"}
    t0 = time.perf_counter()
    try:
        argv = ["bash", "-lc", command]
        if zone:     # a scope execs bash in place, so the pid and process group stay ours to kill
            # OOMPolicy=continue: hitting the zone cap kills the offending process, not the shell
            # that would report it.
            # botlan_sandbox applies the Landlock scope, then execs bash.
            argv = ["systemd-run", "--user", "--scope", "--quiet", "-p", "OOMPolicy=continue",
                    f"--slice={slice_name(zone['id'])}", sys.executable, str(SANDBOX),
                    "--spec", json.dumps(sandbox_spec(zone)), "--", *argv]
        proc = subprocess.Popen(argv, cwd=work, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=EXEC_TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
    finally:
        _EXEC_SLOTS.release()
    truncated = len(out) > EXEC_MAX_OUTPUT
    if zone and not timed_out and proc.returncode in (137, -9, -15):
        out += (f"\n[botlan] killed - most likely the zone memory cap ({zone['mem_gb']:g} GB) was "
                "reached.\n").encode()
    return {"exit_code": None if timed_out else proc.returncode, "timed_out": timed_out,
            "truncated": truncated, "cwd": str(work), "zone": zone["id"] if zone else None,
            "scope_enforced": (SCOPE_ENFORCED and b"[botlan-sandbox] unconfined" not in out[:400])
                              if zone else None,
            "output": out[-EXEC_MAX_OUTPUT:].decode("utf-8", "replace"),
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


def telemetry() -> dict:
    """What the panel shows about this Spark. GB10 memory is one pool, so /proc/meminfo is the
    number that matters; nvidia-smi adds utilisation, temperature and the GPU processes."""
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in ("MemTotal", "MemAvailable"):
                mem[key] = round(int(value.split()[0]) / 1024 / 1024, 2)
    except OSError:
        pass
    gpu, procs = {}, []
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        name, util, temp, power = [x.strip() for x in q.stdout.strip().split(",")[:4]]
        gpu = {"name": name, "util_pct": _num(util), "temp_c": _num(temp), "power_w": _num(power)}
        q = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        for line in q.stdout.strip().splitlines():
            pid, pname, used = [x.strip() for x in line.split(",")[:3]]
            procs.append({"pid": int(pid), "name": Path(pname).name, "mem_mb": _num(used)})
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    load = None
    try:
        load = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError):
        pass
    return {"mem_total_gb": mem.get("MemTotal"), "mem_available_gb": mem.get("MemAvailable"),
            "gpu": gpu, "gpu_processes": procs, "load1": load, "at": int(time.time() * 1000)}


def _num(text: str):
    try:
        return float(text)
    except ValueError:
        return None      # "[N/A]" on GB10 for fields that unified memory makes meaningless


_CTX = {"at": 0.0, "value": None}


def ctx_per_slot() -> int | None:
    """Per-slot context of llama-server, from /props, cached 60 s."""
    if time.time() - _CTX["at"] < 60:
        return _CTX["value"]
    value = None
    try:
        with urllib.request.urlopen(VLM_URL + "/props", timeout=3) as r:
            props = json.loads(r.read())
        n = (props.get("default_generation_settings") or {}).get("n_ctx")
        if not n and props.get("n_ctx") and props.get("total_slots"):
            n = int(props["n_ctx"]) // int(props["total_slots"])
        value = int(n) if n else None
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        pass
    _CTX.update(at=time.time(), value=value)
    return value


def sandbox_spec(zone: dict) -> dict:
    return {"rw": [*zone_scope(zone), zone["workdir"]], "ro": SANDBOX_RO, "protect": PROTECT}


def sandbox_selftest() -> bool:
    """Is the scope really enforced on this kernel? Read the master key file from a sandbox whose
    scope is the whole home: it must fail."""
    keyfile = Path(GW.get("key_file", "~/.spark-duo/botlan.key")).expanduser()
    spec = {"rw": [str(HOME)], "ro": SANDBOX_RO, "protect": PROTECT}
    try:
        r = subprocess.run([sys.executable, str(SANDBOX), "--spec", json.dumps(spec), "--",
                            "cat", str(keyfile)], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode != 0 and b"unconfined" not in r.stderr and KEY.encode() not in r.stdout


def probe(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BotLanGateway/1.0"

    def log_message(self, fmt, *args):   # one line per request, no bodies
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def _send(self, obj, code=200, headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        """Master key -> self.zone is None (whole Spark, manages zones); zone key -> that zone."""
        got = self.headers.get("Authorization", "")
        token = got[7:].strip() if got.lower().startswith("bearer ") else ""
        self.zone = None
        if token and hmac.compare_digest(token.encode(), KEY.encode()):
            return True
        if token:
            digest = _digest(token)
            for zone in load_zones():
                if hmac.compare_digest(digest, zone["key_sha256"]):
                    self.zone = zone
                    return True
        self._send({"error": {"message": "invalid or missing API key", "type": "auth"}}, 401)
        return False

    def do_GET(self):
        if not self._authorized():
            return
        if self.path == "/health":
            self._send({"ok": True, "botlan": 1, "model": MODEL_ID, "vlm": probe(VLM_URL + "/health"),
                        "jev": probe(ORCH_URL + "/health"), "mem_available_gb": mem_available_gb(),
                        "route_threshold": THRESHOLD, "exec": ALLOW_EXEC, "ctx_per_slot": ctx_per_slot(),
                        "scope_enforced": SCOPE_ENFORCED,
                        "exec_root": (zone_scope(self.zone)[0] if self.zone else str(EXEC_ROOT)) if ALLOW_EXEC else None,
                        "zone": public_zone(self.zone) if self.zone else None})
        elif self.path == "/botlan/telemetry":
            data = telemetry()
            if self.zone:
                data["zone"] = public_zone(self.zone, data["gpu_processes"])
            self._send(data)
        elif self.path == "/botlan/zones":
            data = telemetry()
            zones = load_zones()
            total = data["mem_total_gb"] or 0
            self._send({"zones": [public_zone(z, data["gpu_processes"]) for z in ([self.zone] if self.zone else zones)],
                        "admin": self.zone is None, "mem_total_gb": total, "reserved_gb": RESERVED_GB,
                        "assignable_gb": round(total - RESERVED_GB - sum(z["mem_gb"] for z in zones), 1)})
        elif self.path in ("/v1/models", "/models"):
            models = [{"id": MODEL_ID, "object": "model", "owned_by": "botlan",
                       "description": "Jev-0.8B skill router + StepFun GELab-Zero-4B on DGX Spark"}]
            if self.zone and self.zone.get("upstream"):     # the name this key's Bot was paired with
                models.insert(0, {"id": zone_model(self.zone), "object": "model", "owned_by": "botlan",
                                  "description": f"{self.zone['upstream']['backend']} upstream via BotLan"})
            self._send({"object": "list", "data": models})
        elif self.path == "/botlan/routes":
            self._send({"routes": list(ROUTES)})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        zone_delete = re.fullmatch(r"/botlan/zones/(z[a-z0-9]{1,15})/delete", self.path)
        if self.path not in ("/v1/chat/completions", "/chat/completions", "/botlan/exec",
                             "/botlan/zones") and not zone_delete:
            self._send({"error": "not found"}, 404)
            return
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send({"error": {"message": "body missing or too large"}}, 413)
            return
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send({"error": {"message": "invalid JSON"}}, 400)
            return
        if self.path == "/botlan/zones" or zone_delete:
            if self.zone is not None:
                self._send({"error": "only the Spark's master key can manage zones"}, 403)
            elif zone_delete:
                ok = delete_zone(zone_delete.group(1))
                print(f"  zone deleted: {zone_delete.group(1)} {ok}", flush=True)
                self._send({"deleted": ok}, 200 if ok else 404)
            else:
                self._create_zone(req)
            return
        if self.path == "/botlan/exec":
            command = req.get("command")
            if not ALLOW_EXEC:
                self._send({"error": "command execution is off on this Spark (botlan_gateway.allow_exec)"}, 403)
            elif not isinstance(command, str) or not command.strip() or len(command) > 8000:
                self._send({"error": "command must be a non-empty string up to 8000 chars"}, 400)
            else:
                print(f"  exec[{self.zone['id'] if self.zone else 'spark'}]: {command[:200]!r}", flush=True)
                self._send(run_command(command, req.get("cwd"), self.zone))
            return

        messages = req.get("messages") or []
        tool_names = [t.get("function", {}).get("name", "?") for t in req.get("tools") or []]
        print(f"  chat: {len(messages)} msgs, last={messages[-1].get('role') if messages else None}, "
              f"tools={tool_names}", flush=True)
        route = None
        question = needs_route(messages)
        skills = skill_index(messages) if question else {}
        if skills:
            try:
                route = jev_route(question, skills)
                route["used"] = route["skill"] != NONE and route["confidence"] >= THRESHOLD
            except Exception as exc:             # the router is advice: never fail the turn over it
                route = {"error": f"{type(exc).__name__}: {exc}", "used": False}
            ROUTES.append({"at": int(time.time() * 1000), "question": question[:200],
                           "skills": len(skills), **route})
            print(f"  jev route: {route}", flush=True)
            if route.get("used"):
                messages = with_hint(messages, hint(route))
        if self.zone:
            messages = with_hint(messages, zone_note(self.zone))

        target = (self.zone or {}).get("upstream")
        upstream = {**req, "messages": messages}
        headers = {"Content-Type": "application/json"}
        if target:           # this zone's own engine or API: its model name, its key
            upstream["model"] = target["model"]
            if target.get("api_key"):
                headers["Authorization"] = "Bearer " + target["api_key"]
            url = target["base_url"] + "/chat/completions"
        else:
            upstream.pop("model", None)      # llama-server serves one model; keep the panel's name out
            url = VLM_URL + "/v1/chat/completions"
        body = json.dumps(upstream).encode()
        up = urllib.request.Request(url, data=body, headers=headers)
        route_header = {"X-Jev-Route": json.dumps(route, ensure_ascii=True)} if route else {}
        try:
            resp = urllib.request.urlopen(up, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:4000].decode("utf-8", "replace")
            if target and target.get("api_key"):
                detail = detail.replace(target["api_key"], "***")
            self._send({"error": {"message": f"model server: {exc.code} {detail}"}}, exc.code)
            return
        except (urllib.error.URLError, OSError) as exc:
            self._send({"error": {"message": f"model server unreachable: {exc}"}}, 502)
            return

        with resp:
            if not req.get("stream"):
                data = json.loads(resp.read())
                data["model"] = zone_model(self.zone) if self.zone else MODEL_ID
                if route:
                    data["jev_route"] = route
                self._send(data, headers=route_header)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            for k, v in route_header.items():
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True
            try:
                if route:      # an SSE comment: OpenAI clients skip it, a curious client can read it
                    self.wfile.write(f": jev-route {json.dumps(route, ensure_ascii=False)}\n\n".encode())
                for line in resp:
                    self.wfile.write(line)
                    if line in (b"\n", b"\r\n"):
                        self.wfile.flush()
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass       # the panel cancelled; dropping the upstream frees the llama slot


    def _create_zone(self, req):
        name = str(req.get("name") or "").strip()[:40]
        try:
            mem_gb, cpu_pct = float(req.get("mem_gb")), float(req.get("cpu_pct", 100))
        except (TypeError, ValueError):
            mem_gb = cpu_pct = -1.0
        if not name or not 1 <= mem_gb <= 120 or not 5 <= cpu_pct <= 2000:
            self._send({"error": "name, mem_gb (1-120) and cpu_pct (5-2000) are required"}, 400)
            return
        try:
            zone, key = create_zone(name, mem_gb, cpu_pct, req.get("scope"), req.get("upstream"),
                                    req.get("color"))
        except ValueError as exc:
            self._send({"error": str(exc)}, 409)
            return
        print(f"  zone created: {zone['id']} {mem_gb:g}GB {cpu_pct:g}% scope={zone['scope']} "
              f"backend={public_zone(zone)['backend']}", flush=True)
        # The key is shown once; only its sha256 is stored.
        self._send({"zone": public_zone(zone), "api_key": key}, 201)


def main():
    global KEY, SCOPE_ENFORCED
    ap = argparse.ArgumentParser(description="BotLan gateway (Jev skill router + GELab)")
    ap.add_argument("--host", default=GW.get("host", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=GW.get("port", 8091))
    args = ap.parse_args()
    KEY = load_key()
    SCOPE_ENFORCED = sandbox_selftest()
    print(f"botlan gateway on {args.host}:{args.port} -> jev {ORCH_URL}, vlm {VLM_URL}, "
          f"zone scope enforced: {SCOPE_ENFORCED}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    threading.current_thread().name = "botlan-gateway"
    main()
