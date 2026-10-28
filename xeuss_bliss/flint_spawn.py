"""Start Flint with posix_spawn instead of fork (xeuss20).

Problem (found 2026-10-21): BLISS starts Flint with gevent's subprocess.Popen, i.e. fork() + exec().
With an NI-DAQmx task open in the BLISS process (the shutter's output task is always open), the
forked child crashes with SIGSEGV in libc before exec - "Command 'flint' died with SIGSEGV".
Reproduced outside BLISS: gevent subprocess + open NI-DAQmx task -> child signal 11; without a task
or with os.posix_spawn -> fine.

install() replaces FlintClient._proxy_create_flint by a copy of BLISS 2.4's code that starts the
same command (python -m flint -s <session> ...) with the same environment through os.posix_spawn
(no fork of the BLISS process; new process group so Ctrl-C in BLISS does not reach Flint).
The command is "python -m xeuss_bliss.flint_main" (= "-m flint" plus xeuss_bliss.flint_patches).
Flint's stdout/stderr go to ~/bliss/logs/flint.log (BLISS discards them).
"""

import os

import psutil

LOG = "/home/saxs/bliss/logs/flint.log"


class SpawnedProcess:
    """The part of the subprocess.Popen interface that FlintClient uses."""

    def __init__(self, pid):
        self.pid = pid
        self._returncode = None

    @property
    def returncode(self):
        if self._returncode is None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:  # already reaped (e.g. by gevent's child watcher)
                if not psutil.pid_exists(self.pid):
                    self._returncode = 0
                return self._returncode
            if pid == self.pid:
                self._returncode = -os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)
        return self._returncode

    def communicate(self, timeout=None):
        return None, None  # output is in LOG

    def poll(self):
        return self.returncode


def _proxy_create_flint(self):
    """Copy of flint.client.proxy.FlintClient._proxy_create_flint (BLISS 2.4) using os.posix_spawn."""
    from flint.client import proxy as p

    if not os.environ.get("DISPLAY", ""):
        p.FLINT_LOGGER.error("DISPLAY environment variable have to be defined to launch Flint")
        raise RuntimeError("DISPLAY environment variable is not defined")

    p.FLINT_LOGGER.warning("Flint starting...")
    env = dict(os.environ)
    env["BEACON_HOST"] = p._get_beacon_address()
    env["OMP_NUM_THREADS"] = "4"
    if p.poll_patch is not None:
        p.poll_patch.set_ld_preload(env)

    session_name = p.current_session.name
    scan_display = p.ScanDisplay()
    python_bin = scan_display.flint_python_bin
    if python_bin == "":
        python_bin = p.sys.executable
    else:
        p.FLINT_LOGGER.warning("Flint is executed with a custom python binary: %s", python_bin)

    # xeuss_bliss.flint_main = "-m flint" + xeuss_bliss.flint_patches (log side profiles)
    args = [python_bin, "-m", "xeuss_bliss.flint_main", "-s", session_name, *scan_display.extra_args]
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    file_actions = [
        (os.POSIX_SPAWN_OPEN, 0, "/dev/null", os.O_RDONLY, 0),
        (os.POSIX_SPAWN_OPEN, 1, LOG, flags, 0o644),
        (os.POSIX_SPAWN_DUP2, 1, 2),
    ]
    pid = os.posix_spawn(python_bin, args, env, file_actions=file_actions, setpgroup=0)
    return SpawnedProcess(pid)


def install():
    """Make BLISS start Flint with posix_spawn (call once in the session setup)."""
    from flint.client.proxy import FlintClient

    FlintClient._proxy_create_flint = _proxy_create_flint
