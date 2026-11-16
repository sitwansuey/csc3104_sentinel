"""One lock for the heavy live lab experiments on this host, so two never overlap.

The P4.01 four-node acceptance harness (experiments/distributed/kind_acceptance.py
deploy and run), the P4.03 Chaos Mesh install and chaos scenario runs
(experiments/boutique/harness/chaos_recovery.py install, and
scripts/test_boutique_live.py --connected --scenario N2|N3|T1) and the P4.05
fault pairs (experiments/boutique/harness/multi_fault.py, --pair) each take it
for their whole live run. Offline, dry-run, plan and read-only preflight
commands do not.

The lock is the file LOCK_NAME in the system temporary directory, created
atomically (O_CREAT | O_EXCL), holding the holder's label, process id,
command line and start time. A second run is refused while it exists. The
holder removes it when its run ends, also when the run raises or is
interrupted. A lock whose process is no longer running (a run that was
killed) is stale: it is reported with its holder, never taken over silently,
and only removed when the operator passes --break-stale-lock. A lock whose
holder cannot be read (an empty or damaged file) is treated as held, since
its process cannot be checked; the operator deletes it by hand.
"""
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

LOCK_NAME = "sentinel-heavy-lab.lock"
BREAK_OPTION = "--break-stale-lock"


class LabLockError(RuntimeError):
    """Another heavy lab run holds the lock, or a stale lock is left and was not to be broken."""


def default_path():
    return Path(tempfile.gettempdir()) / LOCK_NAME


def _windows_pid_alive(pid):
    """OpenProcess and GetExitCodeProcess: never os.kill, which terminates the process on Windows."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists, owned by someone else.
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # Cannot tell: treat it as running, so the lock is never taken from a live run.
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid):
    """Whether a process with this id is running on this host. Unsure counts as running.

    A dead holder's id may be reused by another process; the lock then looks
    held, which refuses a run rather than letting two overlap.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def describe(info):
    """One line naming a lock's holder, its process id, command and start time."""
    if not isinstance(info, dict) or "pid" not in info:
        return "an unreadable lock file"
    return (f"{info.get('holder', 'unknown')}, pid {info.get('pid')}, started {info.get('started_at', 'unknown')}, "
            f"command: {info.get('command', 'unknown')}")


class HeavyLabLock:
    """The shared heavy-lab lock file (module docstring). Use as a context manager around the whole live run.

    path defaults to LOCK_NAME in the system temporary directory; holder
    names the experiment; break_stale removes a lock whose process is no
    longer running (the operator's --break-stale-lock). alive and pid are
    injectable for tests.
    """

    def __init__(self, path=None, holder="heavy lab run", *, command=None, break_stale=False, alive=None,
                 pid=None):
        self.path = Path(path) if path is not None else default_path()
        self.holder, self.break_stale, self.alive = holder, break_stale, alive or pid_alive
        self.command = command if command is not None else " ".join([Path(sys.argv[0]).name, *sys.argv[1:]])
        self.pid = os.getpid() if pid is None else pid
        self.token, self.held, self.broken = uuid4().hex, False, None

    def holder_of(self):
        """The lock file's contents: None when there is no lock, {} when it cannot be read."""
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {}
        return info if isinstance(info, dict) else {}

    def stale(self, info):
        """A lock whose recorded process is no longer running."""
        return isinstance(info, dict) and isinstance(info.get("pid"), int) and not self.alive(info["pid"])

    def check(self):
        """LabLockError when another run holds the lock or a stale one is left; None when free or ours.

        Read-only: a preflight uses it to refuse early without taking the lock.
        """
        info = self.holder_of()
        if info is None or (self.held and info.get("token") == self.token):
            return
        raise self._refusal(info)

    def _refusal(self, info):
        if self.stale(info):
            return LabLockError(f"A stale heavy-lab lock is left at {self.path} by {describe(info)}; that process "
                                "is no longer running. Check that nothing of that run is still on a cluster, then "
                                f"rerun with {BREAK_OPTION} to remove it")
        return LabLockError(f"Another heavy lab run holds {self.path} ({describe(info)}); run the P4.01, P4.03 and "
                            "P4.05 live experiments one at a time")

    def acquire(self):
        for _ in range(3):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                info = self.holder_of()
                if info is None:
                    continue  # Released between the create and the read: try again.
                if self.stale(info) and self.break_stale:
                    self._break(info)
                    continue
                raise self._refusal(info) from None
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"holder": self.holder, "pid": self.pid, "command": self.command,
                           "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                           "token": self.token}, stream)
            self.held = True
            return self
        raise LabLockError(f"Could not take the heavy-lab lock at {self.path}; it keeps changing")

    def _break(self, info):
        """Remove a stale lock: moved aside first, so a fresh lock another run took meanwhile is put back."""
        aside = self.path.with_name(f"{self.path.name}.stale-{uuid4().hex}")
        try:
            os.rename(self.path, aside)
        except FileNotFoundError:
            return
        try:
            moved = json.loads(aside.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            moved = {}
        if moved != info:
            try:
                os.rename(aside, self.path)
            except OSError:
                raise LabLockError(f"Another run took the heavy-lab lock while the stale one was removed; its lock "
                                   f"was moved to {aside}: put it back at {self.path}") from None
            return
        aside.unlink(missing_ok=True)
        self.broken = info

    def release(self):
        """Remove the lock if this object holds it, and only if the file is still its own."""
        if not self.held:
            return
        self.held = False
        if (self.holder_of() or {}).get("token") == self.token:
            self.path.unlink(missing_ok=True)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()


def run_holding(lock, body, *, stream=None):
    """body() while holding lock; 2 with the refusal on stderr when it is held or stale. Released on any exit."""
    try:
        lock.acquire()
    except LabLockError as error:
        print("Refused: " + str(error), file=stream or sys.stderr, flush=True)
        return 2
    try:
        if lock.broken is not None:
            print(f"Removed a stale heavy-lab lock ({describe(lock.broken)})", file=stream or sys.stderr, flush=True)
        return body()
    finally:
        lock.release()
