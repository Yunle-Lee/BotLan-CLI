#!/usr/bin/env python3
"""Tool-registry checks: the ceilings, the jail and the envelope are the point, not the tools.

Run: python3 agent/test_tools.py   (stdlib only - no venv needed)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools import Registry, ToolError

ROOT = Path(__file__).resolve().parent.parent
failures = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main():
    reg = Registry(root=str(ROOT))
    print("registry: root=%s tools=%s" % (reg.root, sorted(reg.tools)))

    # -- schemas ---------------------------------------------------------------------------
    schemas = reg.schemas()
    check("one OpenAI schema per tool", len(schemas) == 5 and all(s["type"] == "function" for s in schemas))
    check("schema shape", all(set(s["function"]) == {"name", "description", "parameters"} for s in schemas))

    # -- a real read -----------------------------------------------------------------------
    env = reg.call("read_file", '{"path": "config.json"}')
    check("read_file returns the file", env["ok"] and '"agent"' in env["content"], str(env)[:200])
    check("envelope is complete", set(env) == {"ok", "content", "error", "bytes", "ms", "truncated"})
    check("bytes match content", env["bytes"] == len(env["content"].encode()))

    # -- the jail --------------------------------------------------------------------------
    for escape in ("/etc/hostname", "../../etc/passwd", "~/spark-duo/../../etc/passwd"):
        env = reg.call("read_file", json.dumps({"path": escape}))
        check(f"jail refuses {escape}", not env["ok"] and "outside the workspace" in (env["error"] or ""),
              str(env)[:160])

    # -- bad calls never kill the loop ------------------------------------------------------
    check("unknown tool", not reg.call("nope", "{}")["ok"])
    check("malformed arguments", not reg.call("read_file", "{not json")["ok"])
    check("wrong argument name", not reg.call("read_file", '{"file": "config.json"}')["ok"])
    check("non-object arguments", not reg.call("read_file", "[1,2]")["ok"])
    check("bad regex", not reg.call("grep", '{"pattern": "([unclosed"}')["ok"])

    # -- truncation -------------------------------------------------------------------------
    small = Registry(root=str(ROOT), max_bytes=40)
    env = small.call("read_file", '{"path": "config.json"}')
    check("truncates at max_output_bytes", env["ok"] and env["truncated"] and len(env["content"]) <= 60,
          str(env)[:160])

    # -- grep / glob / list_dir --------------------------------------------------------------
    env = reg.call("grep", '{"pattern": "keep_prefix", "path": ".", "glob": "agent/*.py"}')
    check("grep runs inside the root", env["ok"], env["error"] or "")
    env = reg.call("glob", '{"pattern": "scripts/*.sh"}')
    check("glob finds the scripts", env["ok"] and "04_serve.sh" in env["content"], env["content"][:160])
    env = reg.call("list_dir", '{"path": "agent"}')
    check("list_dir lists the agent package", env["ok"] and "tools.py" in env["content"],
          env["content"][:160])

    env = reg.call("http_fetch", '{"url": "file:///etc/passwd"}')
    check("http_fetch refuses file://", not env["ok"], str(env)[:160])
    check("mutating tools are not offered by default",
          "write_file" not in reg.tools and "shell" not in reg.tools, str(sorted(reg.tools)))

    # -- mutations: off, then approval-gated, then allowed ------------------------------------
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        target = '{"path": "out.txt", "content": "hello"}'
        off = Registry(root=tmp, enabled=["write_file", "shell"], allow_write=False)
        check("write_file refused while mutation is disabled",
              not off.call("write_file", target)["ok"], str(off.call("write_file", target))[:160])
        check("shell refused while mutation is disabled",
              not off.call("shell", '{"command": "echo hi"}')["ok"])

        on = Registry(root=tmp, enabled=["write_file", "shell"], allow_write=True,
                      allow_shell=True, require_approval=True)
        env = on.call("write_file", target)
        check("write_file refused without approval", not env["ok"] and "approval" in (env["error"] or ""),
              str(env)[:160])
        env = on.call("write_file", target, approved=True)
        check("write_file runs once approved", env["ok"] and "out.txt" in env["content"], str(env)[:160])
        check("the file is really there", (Path(tmp) / "out.txt").read_text() == "hello")
        env = on.call("shell", '{"command": "echo approved"}', approved=True)
        check("shell runs once approved", env["ok"] and "approved" in env["content"], str(env)[:200])
        env = on.call("write_file", '{"path": "../escape.txt", "content": "x"}', approved=True)
        check("the jail still applies to writes", not env["ok"], str(env)[:160])

        open_reg = Registry(root=tmp, enabled=["write_file"], allow_write=True, require_approval=False)
        check("require_approval=false lets a mutating call through",
              open_reg.call("write_file", target)["ok"])

    print()
    if failures:
        print(f"FAILED: {len(failures)} - {', '.join(failures)}")
        return 1
    print("tool registry ok: jail, envelope, truncation, bad calls, approval")
    check("info reports the switches", set(reg.info()) == {"root", "tools", "max_bytes", "timeout_s",
                                                        "allow_shell", "allow_write", "require_approval"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
