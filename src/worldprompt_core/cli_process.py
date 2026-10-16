"""Spawn helpers for the external provider CLIs: DLL scoping, bounded runs, tree kill.

Provider-independent. ``agent.generate_plan`` (planning), ``agent.check_provider``
(login status) and ``models`` (model discovery) all start CLIs through here or
around ``windows_cli_dll_directory()``.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading


_dll_directory_lock = threading.Lock()


@contextlib.contextmanager
def windows_cli_dll_directory():
    """Let an external CLI spawned inside the block use system libraries (frozen Windows only).

    PyInstaller points the process DLL search path at its bundle directory. For
    the duration of the spawn this clears it with ``SetDllDirectoryW(None)`` and
    then restores whatever was set before, so the calling process, including
    Core's multithreaded GUI process, is left exactly as it was. The setting is
    process-global, hence the lock. A no-op everywhere else.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        yield
        return
    import ctypes
    kernel = ctypes.windll.kernel32
    with _dll_directory_lock:
        # GetDllDirectoryW returns the length needed (with terminator) for a
        # too-small buffer, the copied length on success, and 0 when unset.
        needed = kernel.GetDllDirectoryW(0, None)
        saved = None
        if needed:
            buffer = ctypes.create_unicode_buffer(needed)
            if kernel.GetDllDirectoryW(needed, buffer):
                saved = buffer.value or None
        if not kernel.SetDllDirectoryW(None):
            raise OSError("Could not reset external CLI DLL search path")
        try:
            yield
        finally:
            if not kernel.SetDllDirectoryW(saved):
                raise OSError("Could not restore the DLL search path")


def kill_tree(process: subprocess.Popen, job=None) -> None:
    """Kill the CLI and every descendant it started (best effort, never raises)."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:  # Group already gone (ProcessLookupError) or not ours.
            pass
        return
    if job is not None:
        from .process_lifetime import close_job
        close_job(job)  # Kill-on-close terminates every member of the job.
        return
    try:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        process.kill()


def run_bounded(command: list[str], *, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    """Like ``subprocess.run(capture_output=True)``, but a descendant cannot hang the caller.

    ``subprocess.run`` waits for pipe EOF, so a grandchild that inherited stdout
    or stderr keeps the check blocked after the CLI itself is gone (and on
    Windows its timeout cleanup blocks the same way). Output therefore goes to
    temporary files, so only the CLI's own exit is awaited, and the CLI gets its
    own POSIX process group or a kill-on-close Windows Job Object so the whole
    tree is killed on timeout (the Windows job also ends any straggler when the
    check finishes).
    """
    job = None
    if os.name == "nt":
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        kwargs = {"start_new_session": True}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        with windows_cli_dll_directory():
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, **kwargs)
        try:
            if os.name == "nt":
                try:
                    from .process_lifetime import assign_to_job, close_job, create_kill_on_close_job
                    job = create_kill_on_close_job()
                    try:
                        assign_to_job(job, process.pid)
                    except OSError:
                        close_job(job)
                        job = None
                except OSError:
                    job = None  # Fall back to taskkill /T on timeout.
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(process, job)
                job = None
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise
            streams = []
            for stream in (out, err):
                stream.seek(0)
                streams.append(stream.read(1_000_000).decode("utf-8", errors="replace"))
            return subprocess.CompletedProcess(command, process.returncode, *streams)
        finally:
            if job is not None:
                from .process_lifetime import close_job
                close_job(job)
            if process.poll() is None:
                kill_tree(process)
