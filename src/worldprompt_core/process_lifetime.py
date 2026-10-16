"""Ensure a disposable Windows worker cannot leave an inference process behind."""
from __future__ import annotations

import functools
import os


@functools.lru_cache(maxsize=1)
def _job_api():
    """ctypes bindings for kill-on-close Job Objects (Windows only; built on first use)."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return ctypes, kernel, ExtendedLimits


def create_kill_on_close_job():
    """Create a Job Object that terminates every member when its last handle closes."""
    ctypes, kernel, ExtendedLimits = _job_api()
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise ctypes.WinError(error)
    return handle


def assign_to_job(job, pid=None):
    """Add a process (default: this one) to ``job``; descendants created afterwards join it."""
    ctypes, kernel, _ = _job_api()
    if pid is None:
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    # PROCESS_SET_QUOTA | PROCESS_TERMINATE are the rights job assignment needs.
    process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(process)


def close_job(job):
    """Close the handle; with kill-on-close this terminates every remaining member."""
    _job_api()[1].CloseHandle(job)


def contain_windows_worker():
    """Join a kill-on-close job before spawning a CLI; retain its handle until exit.

    Windows terminates a multiprocessing child without running Python finally
    blocks. A job contains the worker and all subsequently created descendants,
    so cancellation, forced termination and normal worker exit close the last
    handle and stop any remaining CLI processes. No breakaway flag is enabled.
    """
    if os.name != "nt":
        return None
    import multiprocessing
    from multiprocessing.connection import wait
    import threading

    handle = create_kill_on_close_job()
    try:
        assign_to_job(handle)
    except OSError:
        close_job(handle)
        raise

    parent = multiprocessing.parent_process()
    if parent is not None:
        def stop_when_parent_exits():
            wait([parent.sentinel])
            # Closing this process also closes the job, stopping every descendant.
            os._exit(1)
        threading.Thread(target=stop_when_parent_exits,
                         name="worldprompt-parent-watch", daemon=True).start()
    # Never CloseHandle while the worker is alive: that would terminate itself.
    # The kernel closes the non-inheritable handle automatically at process exit.
    return handle
