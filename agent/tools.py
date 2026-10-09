#!/usr/bin/env python3
"""The agent's hands: a small tool registry the loop can call.

Every tool returns the same envelope, so the loop, the rail and the eval never special-case one:

    {"ok": bool, "content": str, "error": str | None, "bytes": int, "ms": float, "truncated": bool}

Three ceilings are structural rather than optional: every path is resolved inside the configured
root (a tool cannot reach outside it), every result is truncated to max_output_bytes, and every call
is timed. Mutating tools carry a flag so the loop can require an explicit approval before running one.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

DEFAULT_ROOT = "~/spark-duo"


class ToolError(Exception):
    """A refusal or a bad call. Never a crash: the loop turns this into an error envelope."""


def envelope(content: str = "", *, ok: bool = True, error: Optional[str] = None,
             ms: float = 0.0, truncated: bool = False) -> dict:
    return {"ok": ok, "content": content, "error": error, "bytes": len(content.encode()),
            "ms": round(ms, 1), "truncated": truncated}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]
    mutating: bool = False
    needs_approval: bool = False
    tags: List[str] = field(default_factory=list)

    def schema(self) -> dict:
        """OpenAI tools entry - this is what goes in the request body verbatim."""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


class Registry:
    """Holds the enabled tools and enforces the ceilings. One instance per orchestrator process."""

    def __init__(self, root: str = DEFAULT_ROOT, enabled: Optional[List[str]] = None,
                 timeout: float = 30.0, max_bytes: int = 60000, max_hits: int = 200,
                 allow_shell: bool = False, allow_write: bool = False,
                 require_approval: bool = True) -> None:
        self.root = Path(root).expanduser().resolve()
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_hits = max_hits
        self.allow_shell = allow_shell
        self.allow_write = allow_write
        self.require_approval = require_approval
        self.tools: Dict[str, Tool] = {}
        for tool in (
            Tool("read_file", "Read a UTF-8 text file inside the workspace.",
                 {"type": "object", "properties": {
                     "path": {"type": "string", "description": "path relative to the workspace root"},
                     "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
                  "required": ["path"]}, self.read_file, tags=["read"]),
            Tool("list_dir", "List a directory inside the workspace.",
                 {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"]}, self.list_dir, tags=["read"]),
            Tool("glob", "Find files by glob pattern, e.g. 'scripts/*.sh'.",
                 {"type": "object", "properties": {"pattern": {"type": "string"},
                                                   "path": {"type": "string"}},
                  "required": ["pattern"]}, self.glob, tags=["read"]),
            Tool("grep", "Search file contents by regular expression.",
                 {"type": "object", "properties": {"pattern": {"type": "string"},
                                                   "path": {"type": "string"},
                                                   "glob": {"type": "string"}},
                  "required": ["pattern"]}, self.grep, tags=["read"]),
            Tool("http_fetch", "GET a URL and return its text (read-only, http/https only).",
                 {"type": "object", "properties": {"url": {"type": "string"}},
                  "required": ["url"]}, self.http_fetch, tags=["net"]),
            # Mutating tools. They are registered but NOT offered unless the config lists them, and
            # even then: allow_write/allow_shell gates the switch and require_approval gates the call.
            Tool("write_file", "Write a text file inside the workspace.",
                 {"type": "object", "properties": {"path": {"type": "string"},
                                                   "content": {"type": "string"}},
                  "required": ["path", "content"]}, self.write_file, mutating=True, tags=["write"]),
            Tool("shell", "Run a shell command in the workspace root.",
                 {"type": "object", "properties": {"command": {"type": "string"},
                                                   "timeout_s": {"type": "number"}},
                  "required": ["command"]}, self.shell, mutating=True, tags=["shell"]),
        ):
            # A mutating tool is never registered by default, whatever the switches say: offering it
            # is a deliberate act (name it in the config), and it still needs allow_write plus an
            # approval before it runs.
            if enabled is None:
                if not tool.mutating:
                    self.tools[tool.name] = tool
            elif tool.name in enabled:
                self.tools[tool.name] = tool

    # -- jail --------------------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        """Resolve inside the root. Escapes are refused, not clamped silently."""
        raw = Path(str(path)).expanduser()
        target = (self.root / raw).resolve() if not raw.is_absolute() else raw.resolve()
        if target != self.root and self.root not in target.parents:
            raise ToolError(f"{path!r} is outside the workspace root {self.root}")
        return target

    def _cap(self, text: str) -> tuple[str, bool]:
        if len(text.encode()) <= self.max_bytes:
            return text, False
        return text.encode()[:self.max_bytes].decode("utf-8", "replace"), True

    # -- tools -------------------------------------------------------------------------------
    def read_file(self, path: str, start_line: Optional[int] = None,
                  end_line: Optional[int] = None) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        lines = target.read_text(errors="replace").splitlines()
        if start_line or end_line:
            lines = lines[(start_line or 1) - 1:end_line]
        head = f"{target.relative_to(self.root) if target != self.root else target} ({len(lines)} lines)\n"
        return head + "\n".join(lines)

    def list_dir(self, path: str = ".") -> str:
        target = self.resolve(path)
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")
        rows = []
        for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if child.name.startswith("."):
                continue
            rows.append(f"{'dir ' if child.is_dir() else 'file'} {child.name}"
                        + (f"  {child.stat().st_size}b" if child.is_file() else ""))
        return "\n".join(rows) or "(empty)"

    def glob(self, pattern: str, path: str = ".") -> str:
        base = self.resolve(path)
        hits = sorted(str(p.relative_to(self.root)) for p in base.glob(pattern)
                      if self.root == p.resolve() or self.root in p.resolve().parents)
        return "\n".join(hits[:self.max_hits]) or "(no match)"

    def grep(self, pattern: str, path: str = ".", glob: str = "**/*") -> str:
        base = self.resolve(path)
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"bad regex: {exc}") from None
        hits: List[str] = []
        for file in sorted(base.glob(glob)):
            if not file.is_file() or any(part.startswith(".") for part in file.parts):
                continue
            try:
                text = file.read_text(errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{file.relative_to(self.root)}:{n}: {line.strip()[:160]}")
                    if len(hits) >= self.max_hits:
                        return "\n".join(hits) + f"\n(stopped at {self.max_hits} hits)"
        return "\n".join(hits) or "(no match)"

    def http_fetch(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            raise ToolError("only http:// and https:// URLs are allowed")
        req = urllib.request.Request(url, headers={"User-Agent": "spark-duo-agent/1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read(self.max_bytes + 1).decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise ToolError(f"{url}: {getattr(exc, 'reason', exc)}") from None

    def write_file(self, path: str, content: str) -> str:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {len(content.encode())} bytes to {target.relative_to(self.root)}"

    def shell(self, command: str, timeout_s: Optional[float] = None) -> str:
        proc = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True,
                              timeout=timeout_s or self.timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        tail = out[-self.max_bytes:] if out else ""
        return f"{tail}\n(exit {proc.returncode})"

    def is_mutating(self, name: str) -> bool:
        """True when this call needs the switch, and with require_approval an approval as well."""
        tool = self.tools.get(name)
        return bool(tool and tool.mutating and (self.allow_write or self.allow_shell))

    # -- dispatch ----------------------------------------------------------------------------
    def schemas(self) -> List[dict]:
        return [t.schema() for t in self.tools.values()]

    def call(self, name: str, arguments, approved: bool = False) -> dict:
        """Run one tool. Always returns an envelope - a bad call must not kill the loop.

        A mutating tool needs the switch ON and, with require_approval, an approval that came from
        outside the loop: the model may propose a write, never authorise one.
        """
        tool = self.tools.get(name)
        if tool is None:
            return envelope(ok=False, error=f"unknown tool {name!r}; have {sorted(self.tools)}")
        if tool.mutating and not (self.allow_write or self.allow_shell):
            return envelope(ok=False, error=f"{name} is a mutating tool and mutation is disabled")
        if tool.mutating and self.require_approval and not approved:
            return envelope(ok=False, error=f"{name} needs explicit approval for this call")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                return envelope(ok=False, error=f"arguments are not valid JSON: {exc}")
        if not isinstance(arguments, dict):
            return envelope(ok=False, error="arguments must be a JSON object")
        t0 = time.perf_counter()
        try:
            content = tool.fn(**arguments)
        except ToolError as exc:
            return envelope(ok=False, error=str(exc), ms=(time.perf_counter() - t0) * 1000)
        except TypeError as exc:
            return envelope(ok=False, error=f"bad arguments for {name}: {exc}",
                            ms=(time.perf_counter() - t0) * 1000)
        except Exception as exc:                      # a tool must never take the loop down
            return envelope(ok=False, error=f"{type(exc).__name__}: {exc}",
                            ms=(time.perf_counter() - t0) * 1000)
        text, cut = self._cap(str(content))
        return envelope(text, ms=(time.perf_counter() - t0) * 1000, truncated=cut)

    def info(self) -> dict:
        return {"root": str(self.root), "tools": sorted(self.tools), "max_bytes": self.max_bytes,
                "timeout_s": self.timeout, "allow_shell": self.allow_shell,
                "allow_write": self.allow_write, "require_approval": self.require_approval}
