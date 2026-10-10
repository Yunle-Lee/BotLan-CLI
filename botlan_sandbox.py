#!/usr/bin/env python3
"""Run one command confined to a Bot's scope, without root: Landlock filesystem rules, then exec.

Why not bwrap / unshare / systemd ProtectHome: on Ubuntu 24.04 with
kernel.apparmor_restrict_unprivileged_userns=1 an unprivileged user cannot write a uid_map, so
every namespace-based sandbox fails (measured on spark-8691: bwrap "setting up uid map: Permission
denied", systemd-run -p TemporaryFileSystem= exits 226/NAMESPACE). Landlock needs no namespace and
no privilege, only no_new_privs.

What the confined process gets:
  read+write   the scope dirs and the zone work dir (minus the protected paths), /tmp, /var/tmp,
               /dev (GPU ioctls)
  read+exec    the rest of / except /home and /root, plus `ro` paths (~/.profile, ~/llama.cpp)
  nothing      everything else under /home - the master key (~/.spark-duo), ~/.ssh, dotfiles,
               the gateway code
  no setuid    no_new_privs: sudo and other setuid binaries cannot elevate

Not covered (filesystem rules only): network, and connecting to an existing UNIX socket such as
the systemd user bus - a command could still ask the user manager to start an unconfined unit.

Landlock only allows, it cannot carve a hole, so a scope that contains a protected path (the
default scope is the whole $HOME) is expanded: every child is granted except the protected ones,
descending only into directories that have a protected path below them.

If the kernel has no Landlock, or a rule cannot be applied, the command runs unconfined and the
first output line says so ("[botlan-sandbox] unconfined: ...").

  python3 botlan_sandbox.py --spec '<json>' -- bash -c 'command'
      spec: {"rw": [dirs], "ro": [dirs], "protect": [paths]}
  python3 botlan_sandbox.py --abi          print the kernel's Landlock ABI (0 = none)
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from pathlib import Path

_CREATE, _ADD_RULE, _RESTRICT = 444, 445, 446      # same numbers on aarch64 and x86_64
_PATH_BENEATH = 1
_VERSION = 1                                        # landlock_create_ruleset flag: query the ABI
_PR_SET_NO_NEW_PRIVS = 38

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REFER, TRUNCATE, IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
RO = EXECUTE | READ_FILE | READ_DIR
FILE_ONLY = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV   # rights valid on a file
MARK = "[botlan-sandbox]"

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def abi() -> int:
    """The kernel's Landlock ABI, 0 when Landlock is missing or disabled."""
    n = _libc.syscall(_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(_VERSION))
    return max(int(n), 0)


def handled(version: int) -> int:
    mask = (1 << 13) - 1                    # ABI 1: EXECUTE .. MAKE_SYM
    if version >= 2:
        mask |= REFER
    if version >= 3:
        mask |= TRUNCATE
    if version >= 5:
        mask |= IOCTL_DEV
    return mask


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def grants(rw: list[str], ro: list[str], protect: list[str]) -> list[tuple[str, str]]:
    """(path, "rw"|"ro") rules. A read+write dir that contains a protected path is replaced by its
    children, recursively, minus the protected ones. Read-only paths are granted as given."""
    prot = [Path(p).resolve() for p in protect]
    out: list[tuple[str, str]] = []

    def add(path: Path, mode: str) -> None:
        if any(_under(path, p) for p in prot):
            return                                  # the dir itself is protected (or inside one)
        if not any(p != path and path in p.parents for p in prot):
            out.append((str(path), mode))
            return
        try:
            children = list(path.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_symlink():
                continue                            # the target is granted (or not) on its own
            add(child, mode)

    for p in rw:
        add(Path(p).resolve(), "rw")
    return out + [(str(Path(p).resolve()), "ro") for p in ro]


def system_ro() -> list[str]:
    """Every top-level dir except /home and /root (read+exec)."""
    skip = {"/home", "/root", "/tmp", "/var", "/dev", "/proc", "/lost+found"}
    out = [str(p) for p in Path("/").iterdir() if str(p) not in skip and not p.is_symlink()]
    out += [str(p) for p in Path("/var").iterdir() if p.name != "tmp" and not p.is_symlink()]
    return out + ["/proc"]


def confine(spec: dict) -> None:
    version = abi()
    if version < 1:
        raise OSError("kernel has no Landlock")
    mask = handled(version)
    attr = _RulesetAttr(mask)
    fd = _libc.syscall(_CREATE, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)),
                       ctypes.c_uint32(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    rw = [*spec.get("rw", []), "/tmp", "/var/tmp", "/dev"]
    ro = [*system_ro(), *spec.get("ro", [])]
    rules = grants([p for p in rw if os.path.isdir(p)], [p for p in ro if os.path.exists(p)],
                   spec.get("protect", []))
    try:
        for path, mode in rules:
            access = mask if mode == "rw" else RO
            try:
                pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                if not os.path.isdir(path):
                    access &= FILE_ONLY
                rule = _PathBeneath(access & mask, pfd)
                if _libc.syscall(_ADD_RULE, ctypes.c_int(fd), ctypes.c_int(_PATH_BENEATH),
                                 ctypes.byref(rule), ctypes.c_uint32(0)) != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")
            finally:
                os.close(pfd)
        if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS)")
        if _libc.syscall(_RESTRICT, ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)


def main() -> None:
    ap = argparse.ArgumentParser(description="run a command inside a Landlock scope")
    ap.add_argument("--spec", default="{}")
    ap.add_argument("--abi", action="store_true")
    ap.add_argument("argv", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    if args.abi:
        print(abi())
        return
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        ap.error("no command")
    try:
        confine(json.loads(args.spec))
    except (OSError, ValueError) as exc:          # never block the command over the sandbox
        sys.stderr.write(f"{MARK} unconfined: {exc}\n")
        sys.stderr.flush()
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
