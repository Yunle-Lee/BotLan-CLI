#!/usr/bin/env python3
"""First-run setup for BotLan on this DGX Spark: one Bot, its scope, its model, its budget.

  python3 botlan_setup.py                 the wizard (Textual if installed, plain prompts otherwise)
  python3 botlan_setup.py pair --json     what the BotLan app needs to connect (non-interactive)
  python3 botlan_setup.py status --json   {"installed", "gateway_ok", "models_ok"}
  python3 botlan_setup.py list [--json]
  python3 botlan_setup.py remove <id>
  python3 botlan_setup.py create --name N --color C --mem-gb G --cpu-pct P --scope DIR ...
                                 --backend jev-step|local|api [--base-url --model --api-key] --json

Everything except the wizard is stdlib only, for the system python3. A Bot is a gateway zone
(botlan_gateway.py): a systemd slice for its budget, a Landlock scope for its commands, and a key.
The key is shown by the gateway once; this tool keeps it in ~/.spark-duo/pair.json (0600) so the
laptop can pair later.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
GW = CONFIG.get("botlan_gateway") or {}
PORT = int(GW.get("port", 8091))
GATEWAY = f"http://127.0.0.1:{PORT}"
KEY_FILE = Path(GW.get("key_file", "~/.spark-duo/botlan.key")).expanduser()
ZONES_FILE = Path(GW.get("zones_file", "~/.spark-duo/zones.json")).expanduser()
STATE = KEY_FILE.parent
PAIR_FILE = STATE / "pair.json"
SCAN_CACHE = STATE / "scan-cache.json"
LOGS = ROOT / "logs"
MODEL_ID = GW.get("model", "jev-step")
COLORS = ["#76B900", "#54A8FF", "#FF7A59", "#C792EA", "#FFD866", "#FF6188"]
LOCAL_PORTS = [8000, 8355, 8356, 30000, 11434, 8080]
BACKENDS = ("jev-step", "local", "api")


# ---- gateway --------------------------------------------------------------------------------

def master_key() -> str | None:
    env = os.environ.get("BOTLAN_GATEWAY_KEY", "").strip()
    if env:
        return env
    try:
        return KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def api(method: str, path: str, body: dict | None = None, timeout: float = 10) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GATEWAY + path, data=data, method=method, headers={
        "Authorization": f"Bearer {master_key() or ''}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, {"error": f"gateway unreachable: {exc}"}


def health() -> dict | None:
    code, data = api("GET", "/health", timeout=6)
    return data if code == 200 else None


def _private_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def pair_keys() -> dict:
    try:
        return json.loads(PAIR_FILE.read_text()).get("keys", {})
    except (OSError, ValueError):
        return {}


def save_pair_key(zone_id: str, key: str | None) -> None:
    keys = pair_keys()
    if key:
        keys[zone_id] = key
    else:
        keys.pop(zone_id, None)
    _private_write(PAIR_FILE, {"keys": keys})


def zones() -> list[dict]:
    """Public zone records from the gateway; the zones file when the gateway is down."""
    code, data = api("GET", "/botlan/zones")
    if code == 200:
        return data.get("zones", [])
    try:
        raw = json.loads(ZONES_FILE.read_text()).get("zones", [])
    except (OSError, ValueError):
        return []
    out = []
    for z in raw:
        up = z.get("upstream")
        out.append({"id": z["id"], "name": z["name"], "color": z.get("color"),
                    "mem_gb": z["mem_gb"], "cpu_pct": z["cpu_pct"],
                    "scope": z.get("scope") or [str(Path.home())],
                    "backend": up["backend"] if up else "jev-step",
                    "model": up["model"] if up else MODEL_ID})
    return out


def budget() -> dict:
    code, data = api("GET", "/botlan/zones")
    if code == 200:
        return {k: data.get(k) for k in ("mem_total_gb", "reserved_gb", "assignable_gb")}
    return {"mem_total_gb": mem_total_gb(), "reserved_gb": GW.get("reserved_gb", 16),
            "assignable_gb": None}


def create_bot(name: str, color: str, mem_gb: float, cpu_pct: float, scope: list[str],
               backend: str, base_url: str = "", model: str = "", api_key: str = "") -> dict:
    """Create the zone; keep its key for pairing. Returns {zone, api_key} or {error}."""
    if backend not in BACKENDS:
        return {"error": f"backend must be one of {', '.join(BACKENDS)}"}
    upstream = None
    if backend != "jev-step":
        if not base_url or not model:
            return {"error": f"--backend {backend} needs --base-url and --model"}
        upstream = {"base_url": base_url, "model": model, "api_key": api_key, "backend": backend}
    body = {"name": name, "color": color, "mem_gb": mem_gb, "cpu_pct": cpu_pct,
            "scope": [str(Path(d).expanduser().resolve()) for d in scope] or None,
            "upstream": upstream}
    code, data = api("POST", "/botlan/zones", body, timeout=30)
    if code != 201:
        return {"error": data.get("error") or f"gateway answered {code}"}
    save_pair_key(data["zone"]["id"], data["api_key"])
    return data


def remove_bot(zone_id: str) -> dict:
    code, data = api("POST", f"/botlan/zones/{zone_id}/delete", {}, timeout=30)
    if code == 200:
        save_pair_key(zone_id, None)
    return data if code else {"error": data.get("error")}


# ---- this machine ---------------------------------------------------------------------------

def mem_total_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return None


def gpu_name() -> str | None:
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5)
        return q.stdout.strip().splitlines()[0].strip() or None
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def pair() -> dict:
    h = health()
    keys = pair_keys()
    bots = [{"id": z["id"], "name": z["name"], "color": z.get("color"), "model": z.get("model", MODEL_ID),
             "api_key": keys.get(z["id"]), "backend": z.get("backend", "jev-step"), "scope": z.get("scope")}
            for z in zones()]
    return {"v": 1,
            "spark": {"hostname": socket.gethostname(), "gateway_port": PORT, "gateway_ok": h is not None,
                      "mem_total_gb": mem_total_gb(), "gpu": gpu_name()},
            "bots": bots,
            "master": {"name": "DGX Spark", "model": MODEL_ID, "api_key": master_key()}}


def status() -> dict:
    h = health()
    installed = (ROOT / "botlan_gateway.py").exists() and master_key() is not None
    return {"installed": installed, "gateway_ok": h is not None,
            "models_ok": bool(h and h.get("vlm") and h.get("jev"))}


def probe_models(base: str, timeout: float = 1.5) -> list[str]:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=timeout) as r:
            data = json.loads(r.read())
        return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return []


def probe_local() -> list[dict]:
    """OpenAI-compatible servers on the usual local ports, in LOCAL_PORTS order."""
    bases = [f"http://127.0.0.1:{p}/v1" for p in LOCAL_PORTS]
    with ThreadPoolExecutor(len(bases)) as pool:
        found = list(pool.map(probe_models, bases))
    return [{"base_url": b, "models": m} for b, m in zip(bases, found) if m]


# ---- the local Jev-Step stack ---------------------------------------------------------------

def stack_state() -> dict:
    """What exists of our own stack, so the wizard reuses it instead of rebuilding."""
    lc = Path(os.environ.get("LC", "~/llama.cpp")).expanduser()
    models = ROOT / "models"
    h = health()
    return {"llama_built": (lc / "build-cuda" / "bin" / "llama-server").exists(),
            "gelab_converted": (models / "gelab-zero-4b-Q4_K_M.gguf").exists(),
            "gateway_ok": h is not None, "models_ok": bool(h and h.get("vlm") and h.get("jev"))}


def stack_steps(state: dict) -> list[str]:
    steps = []
    if not state["llama_built"]:
        steps.append("scripts/01_build_llamacpp_cuda.sh")
    if not state["gelab_converted"]:
        steps.append("scripts/03_convert_gelab.sh")
    if not state["models_ok"]:
        steps.append("scripts/04_serve.sh")
    if not state["gateway_ok"]:
        steps.append("scripts/08_botlan.sh --install")
    return steps


def start_stack(steps: list[str]) -> subprocess.Popen:
    """Run the missing steps one after another, detached, logging to logs/setup-stack.log."""
    LOGS.mkdir(parents=True, exist_ok=True)
    script = " && ".join(f'echo "== {s}" && sh {s}' for s in steps) + ' && echo "== done"'
    log = open(LOGS / "setup-stack.log", "ab")
    return subprocess.Popen(["sh", "-c", script], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)


# ---- scope: a light home scan + treemap -----------------------------------------------------

def _walk(path: str, deadline: float) -> tuple[int, bool]:
    """Bytes under path (no symlinks, one filesystem); False when cut by the deadline."""
    total, stack = 0, [path]
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return 0, True
    while stack:
        if time.monotonic() > deadline:
            return total, False
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if st.st_dev == dev:
                            stack.append(e.path)
                    else:
                        total += st.st_blocks * 512
        except OSError:
            continue                                  # unreadable: skip
    return total, True


def scan_home(budget_s: float = 4.0, per_dir_s: float = 1.5, use_cache: bool = True,
              home: str | None = None) -> dict:
    """Two levels under $HOME with sizes. A dir cut by its timeout is marked partial (its size is
    a lower bound). Results are cached; a cached size is kept when a fresh scan was cut."""
    home = home or str(Path.home())
    try:
        cache = json.loads(SCAN_CACHE.read_text()) if use_cache else {}
    except (OSError, ValueError):
        cache = {}
    if cache.get("home") == home and time.time() - cache.get("at", 0) < 3600:
        return cache
    old = {d["path"]: d for d in cache.get("dirs", [])}
    start = time.monotonic()
    end = start + budget_s
    try:
        tops = sorted(e.path for e in os.scandir(home)
                      if e.is_dir(follow_symlinks=False) and not e.name.startswith("."))
    except OSError:
        tops = []

    def one(top: str) -> dict:
        kids, complete, size = [], True, 0
        try:
            entries = list(os.scandir(top))
        except OSError:
            return {"path": top, "size": 0, "partial": True, "children": []}
        dir_deadline = min(end, time.monotonic() + per_dir_s)
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    n, ok = _walk(e.path, dir_deadline)
                    kids.append({"path": e.path, "size": n, "partial": not ok})
                elif not e.is_symlink():
                    n, ok = e.stat(follow_symlinks=False).st_blocks * 512, True
                else:
                    continue
            except OSError:
                continue
            size += n
            complete &= ok
        prev = old.get(top)
        if not complete and prev and prev["size"] > size:
            return prev                                # a partial scan never shrinks a known size
        kids.sort(key=lambda k: -k["size"])
        return {"path": top, "size": size, "partial": not complete, "children": kids[:12]}

    with ThreadPoolExecutor(8) as pool:
        dirs = list(pool.map(one, tops))
    dirs.sort(key=lambda d: -d["size"])
    out = {"home": home, "at": time.time(), "dirs": dirs, "ms": round((time.monotonic() - start) * 1000)}
    try:
        _private_write(SCAN_CACHE, out)
    except OSError:
        pass
    return out


def human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit in ("B", "K") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def layout(items: list[tuple[str, float]], x: int, y: int, w: int, h: int) -> list[tuple]:
    """Treemap by halves: split the list into two groups of about equal size, the rectangle
    along its longer side, recurse. Returns (label, x, y, w, h) with area ~ size."""
    items = [(k, max(v, 1.0)) for k, v in items if w > 0 and h > 0]
    if not items:
        return []
    if len(items) == 1:
        return [(items[0][0], x, y, w, h)]
    total, acc, cut = sum(v for _, v in items), 0.0, 1
    for i, (_, v) in enumerate(items[:-1]):
        acc += v
        cut = i + 1
        if acc >= total / 2:
            break
    left = sum(v for _, v in items[:cut]) / total
    if w >= h * 2:                     # a cell is about twice as tall as wide
        lw = max(1, min(w - 1, round(w * left)))
        return layout(items[:cut], x, y, lw, h) + layout(items[cut:], x + lw, y, w - lw, h)
    lh = max(1, min(h - 1, round(h * left)))
    return layout(items[:cut], x, y, w, lh) + layout(items[cut:], x, y + lh, w, h - lh)


def treemap_cells(dirs: list[dict], width: int, height: int, limit: int = 14) -> tuple[list, list]:
    """A char grid [[(char, rect index)]] and the rects, for the biggest `limit` dirs (+ rest)."""
    items = [(d["path"], d["size"]) for d in dirs[:limit]]
    rest = sum(d["size"] for d in dirs[limit:])
    if rest:
        items.append(("…", rest))
    rects = layout(items, 0, 0, width, height)
    sizes = dict(items)
    grid = [[(" ", -1)] * width for _ in range(height)]
    for i, (path, x, y, w, h) in enumerate(rects):
        for r in range(y, y + h):
            for c in range(x, x + w):
                ch = " "
                if r == y and c == x:
                    ch = "┌"
                elif r == y:
                    ch = "─"
                elif c == x:
                    ch = "│"
                grid[r][c] = (ch, i)
        label = f"{Path(path).name or path} {human(sizes[path])}"
        if h >= 2 and w >= 3:
            for j, ch in enumerate(label[: w - 1]):
                grid[y + 1][x + 1 + j] = (ch, i)
        elif w >= 3:
            for j, ch in enumerate(label[: w - 1]):
                grid[y][x + 1 + j] = (ch, i)
    return grid, rects


def treemap_text(dirs: list[dict], width: int = 72, height: int = 16) -> str:
    grid, _ = treemap_cells(dirs, width, height)
    return "\n".join("".join(ch for ch, _ in row) for row in grid)


# ---- the plain wizard (no Textual) ----------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    got = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return got or default


def plain_wizard() -> int:
    print("BotLan setup - your first Bot on this DGX Spark\n")
    name = _ask("1. Bot name", "Vision")
    for i, c in enumerate(COLORS, 1):
        print(f"   {i}. {c}")
    color = COLORS[int(_ask("   color", "1")) - 1]

    print("\n2. Activity scope (what its commands may read and write)")
    scan = scan_home()
    print(treemap_text(scan["dirs"]))
    home = scan["home"]
    for i, d in enumerate(scan["dirs"][:20], 1):
        print(f"   {i:2}. {Path(d['path']).name:<28} {human(d['size']):>8}{'+' if d['partial'] else ''}")
    pick = _ask("   numbers or paths, space-separated (empty = whole home)", "")
    scope = []
    for tok in pick.split():
        scope.append(scan["dirs"][int(tok) - 1]["path"] if tok.isdigit() else tok)
    scope = scope or [home]

    print("\n3. Model backend: jev-step (this Spark's stack) | local | api")
    backend = _ask("   backend", "jev-step")
    base_url = model = api_key = ""
    if backend == "jev-step":
        steps = stack_steps(stack_state())
        if steps:
            print(f"   missing: {', '.join(steps)} - starting them in the background "
                  f"(log: {LOGS / 'setup-stack.log'})")
            start_stack(steps).wait()
        else:
            print("   found: llama-server + Jev orchestrator + gateway, reusing them")
    elif backend == "local":
        found = probe_local()
        for i, f in enumerate(found, 1):
            print(f"   {i}. {f['base_url']}  {', '.join(f['models'][:3])}")
        if not found:
            print("   no OpenAI-compatible server on " + ", ".join(map(str, LOCAL_PORTS)))
            return 1
        f = found[int(_ask("   which", "1")) - 1]
        base_url, model = f["base_url"], _ask("   model", f["models"][0])
    else:
        base_url = _ask("   base URL (…/v1)")
        model = _ask("   model")
        api_key = _ask("   API key")

    b = budget()
    print(f"\n4. Resources: {b['assignable_gb']} GB assignable of {b['mem_total_gb']} GB "
          f"({b['reserved_gb']} GB kept for Spark Duo and the OS)")
    mem = float(_ask("   memory GB", str(min(16, int(b["assignable_gb"] or 16)))))
    cpu = float(_ask("   CPU % (100 = one core)", "400"))

    res = create_bot(name, color, mem, cpu, scope, backend, base_url, model, api_key)
    if "error" in res:
        print(f"\nnot created: {res['error']}")
        return 1
    print("\n5. Created.\n" + pairing_text(res))
    return 0


def pairing_text(res: dict) -> str:
    z = res["zone"]
    return (f"   Bot      {z['name']} ({z['id']}) {z.get('color') or ''}\n"
            f"   scope    {', '.join(z['scope'])}  (enforced: {z.get('scope_enforced')})\n"
            f"   backend  {z['backend']}  model {z['model']}\n"
            f"   key      {res['api_key']}  (saved for pairing in {PAIR_FILE})\n\n"
            "   Next, on the laptop: open BotLan -> Connect DGX Spark. It runs\n"
            f"     ssh <spark> python3 {ROOT}/botlan_setup.py pair --json\n"
            f"   and keeps the tunnel up (ssh -N -L {PORT}:127.0.0.1:{PORT} <spark>).\n"
            f"   Manual: Bot base URL http://127.0.0.1:{PORT}/v1, model {z['model']}, the key above.")


# ---- cli ------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="BotLan first-run setup on this DGX Spark")
    ap.add_argument("--plain", action="store_true", help="prompts instead of the Textual wizard")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("pair", "status", "list"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    sub.add_parser("remove").add_argument("id")
    c = sub.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--color", default=COLORS[0])
    c.add_argument("--mem-gb", type=float, default=8)
    c.add_argument("--cpu-pct", type=float, default=400)
    c.add_argument("--scope", nargs="+", default=[])
    c.add_argument("--backend", choices=BACKENDS, default="jev-step")
    c.add_argument("--base-url", default="")
    c.add_argument("--model", default="")
    c.add_argument("--api-key", default="")
    c.add_argument("--json", action="store_true")
    sub.add_parser("scan").add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    if args.cmd == "pair":
        print(json.dumps(pair(), ensure_ascii=False))
        return 0
    if args.cmd == "status":
        print(json.dumps(status()))
        return 0
    if args.cmd == "list":
        zs = zones()
        if args.json:
            print(json.dumps(zs, ensure_ascii=False))
        for z in [] if args.json else zs:
            print(f"{z['id']:<16} {z['name']:<16} {z.get('backend', 'jev-step'):<9} "
                  f"{z['mem_gb']:>5g} GB {z['cpu_pct']:>5g}%  {', '.join(z.get('scope') or [])}")
        return 0
    if args.cmd == "remove":
        res = remove_bot(args.id)
        print(json.dumps(res))
        return 0 if res.get("deleted") else 1
    if args.cmd == "create":
        res = create_bot(args.name, args.color, args.mem_gb, args.cpu_pct, args.scope, args.backend,
                         args.base_url, args.model, args.api_key)
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(res["error"] if "error" in res else pairing_text(res))
        return 1 if "error" in res else 0
    if args.cmd == "scan":
        s = scan_home(use_cache=not args.fresh)
        print(treemap_text(s["dirs"]))
        print(f"{len(s['dirs'])} dirs in {s.get('ms')} ms")
        return 0
    if not args.plain:
        try:
            from tui.setup_wizard import run
        except ImportError:
            pass
        else:
            return run()
    return plain_wizard()


if __name__ == "__main__":
    sys.exit(main())
