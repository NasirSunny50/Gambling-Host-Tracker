"""Answer, in one run, why the portal will not start.

Three different faults produce the same WinError 10048, and telling them apart by hand
takes four commands whose output has to be cross-read:

  1. An old portal is still holding the port  -> there is a PID to kill.
  2. Windows has bind-reserved the port for Hyper-V / WSL / Docker -> nothing is listening
     and nothing can be killed; the only fix is a different port.
  3. The launcher on disk is older than the fix  -> the port was never the problem.

The third one is invisible from the error message and is the one that wasted the most
time: the window kept printing an old banner while the fix sat unmerged. So this checks
the files on disk as well as the machine, and ends with a verdict rather than raw output.

Read-only. It kills nothing and changes nothing.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PORT = 8000
HOST = "127.0.0.1"

# Strings that exist only in the fixed versions of the two files. Their absence means the
# working copy predates the fix, whatever git says about the branch.
MARKERS = {
    "scripts/Start.bat": "Starting the portal and opening it",
    "scripts/internal/serve.py": "_resolve_port",
}


def _run(cmd: str) -> str:
    """Run a shell command and return its output, or an empty string if it cannot run."""
    try:
        # shell=True on fixed command strings only - nothing here comes from a caller.
        done = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30, check=False
        )
        return (done.stdout or "") + (done.stderr or "")
    except Exception:  # noqa: BLE001 - a check that cannot run is a blank line, never a crash
        return ""


def _head(title: str) -> None:
    print()
    print("-" * 66)
    print(title)
    print("-" * 66)


def _port_is_free(host: str, port: int) -> bool:
    """Try the bind uvicorn is about to try. Nothing else answers the same question: a
    reserved port has no listener yet still refuses the bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def check_files() -> bool:
    """Whether the working copy actually carries the fix."""
    _head("1. Is the fixed code on disk?")
    ok = True
    for rel, marker in MARKERS.items():
        path = REPO_ROOT / rel
        if not path.exists():
            print(f"  MISSING   {rel}")
            ok = False
            continue
        has = marker in path.read_text(encoding="utf-8", errors="replace")
        print(f"  {'CURRENT ' if has else 'OLD     '}  {rel}")
        ok = ok and has

    print(f"\n  Folder: {REPO_ROOT}")
    log = _run(f'git -C "{REPO_ROOT}" log --oneline -3').strip()
    branch = _run(f'git -C "{REPO_ROOT}" rev-parse --abbrev-ref HEAD').strip()
    # git writes its complaints to stderr, which we captured too, so a non-repo folder
    # would otherwise be reported as a branch literally named "fatal: ...".
    if "fatal:" in log or not log:
        print("  Not a git checkout - so this is not the folder git is updating.")
    else:
        print("  Last 3 commits:")
        for line in log.splitlines():
            print(f"    {line}")
        if branch and "fatal:" not in branch:
            print(f"  Branch: {branch}")
    return ok


def check_port() -> bool:
    """Whether port 8000 can actually be bound right now."""
    _head(f"2. Can the portal bind {HOST}:{PORT}?")
    free = _port_is_free(HOST, PORT)
    print(f"  Bind test: {'FREE - the port is usable' if free else 'REFUSED - this is the 10048'}")
    return free


def check_listener() -> bool:
    """Whether a real process holds the port. Returns True if one was found."""
    _head("3. Is a process holding it?")
    if not sys.platform.startswith("win"):
        print("  (Windows only - skipped on this machine.)")
        return False

    rows = [ln for ln in _run("netstat -ano").splitlines()
            if f":{PORT} " in ln and "LISTENING" in ln.upper()]
    if not rows:
        print("  Nothing is LISTENING on the port.")
        print("  So there is no process to kill - see the reserved ranges below.")
        return False

    pids = set()
    for row in rows:
        print(f"  {row.strip()}")
        parts = row.split()
        if parts and parts[-1].isdigit():
            pids.add(parts[-1])
    for pid in sorted(pids):
        name = _run(f'tasklist /FI "PID eq {pid}" /NH').strip().splitlines()
        print(f"\n  PID {pid}: {name[-1].strip() if name else 'unknown'}")
        print(f"  To free it:  taskkill /F /PID {pid}")
    return True


def check_reserved() -> bool:
    """Whether Windows has bind-reserved the port. Returns True if it has."""
    _head("4. Has Windows reserved the port?")
    if not sys.platform.startswith("win"):
        print("  (Windows only - skipped on this machine.)")
        return False

    out = _run("netsh interface ipv4 show excludedportrange protocol=tcp")
    reserved = False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            low, high = int(parts[0]), int(parts[1])
            hit = low <= PORT <= high
            reserved = reserved or hit
            print(f"  {low:>6} - {high:<6}{'   <-- 8000 IS IN THIS RANGE' if hit else ''}")
    if reserved:
        print("\n  Windows holds this port for Hyper-V / WSL / Docker.")
        print("  Nothing is running and nothing can be killed. Another port is the only fix.")
    else:
        print("  Port 8000 is not in a reserved range.")
    return reserved


def check_leftovers() -> None:
    """Python and browser processes a killed run may have left behind."""
    _head("5. Leftover processes")
    if not sys.platform.startswith("win"):
        print("  (Windows only - skipped on this machine.)")
        return
    for image in ("python.exe", "chrome.exe", "msedge.exe"):
        rows = [ln for ln in _run(f'tasklist /FI "IMAGENAME eq {image}" /NH').splitlines()
                if image in ln]
        print(f"  {image:<12} {len(rows)} running")


def suggest_port() -> int | None:
    """The port the portal would move to."""
    for candidate in range(PORT + 1, PORT + 51):
        if _port_is_free(HOST, candidate):
            return candidate
    return None


def main() -> int:
    print("=" * 66)
    print("  Host Tracker - why will the portal not start?")
    print("=" * 66)

    code_is_current = check_files()
    port_is_free = check_port()
    has_listener = check_listener() if not port_is_free else False
    is_reserved = check_reserved() if not port_is_free else False
    check_leftovers()

    _head("VERDICT")
    if not code_is_current:
        print("  The launcher on disk is OLDER than the fix.")
        print("  That is the problem to solve first - the port fix cannot run if it is")
        print("  not there. In this folder, run:")
        print("      git fetch origin")
        print("      git checkout main")
        print("      git merge --ff-only origin/claude/gambling-host-tracker-work-t9e254")
        print("  Then start it again.")
    elif port_is_free:
        print("  Port 8000 is free and the code is current. The portal should start.")
        print("  If it still fails, something took the port between this check and the")
        print("  start - run this again immediately after a failure.")
    elif has_listener:
        print("  A real process holds port 8000. Kill it with the taskkill line above,")
        print("  or just start the portal - the current code steps to the next free port.")
    elif is_reserved:
        alt = suggest_port()
        print("  Windows has reserved port 8000. There is nothing to kill.")
        print(f"  The current code handles this by moving to the next free port"
              f"{f' - {alt} is open right now' if alt else ''}.")
        print("  Seeing 10048 anyway means the running launcher is not the fixed one.")
    else:
        print("  The bind is refused, nothing is listening, and no reserved range covers")
        print("  it. Send this whole output - the cause is not one of the usual three.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
