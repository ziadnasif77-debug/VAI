"""Peak memory measurement, for the §7 acceptance test.

§7 requires an eight-hour recording to be processed **without loading the video
into RAM**. That is a claim about memory, so it is checked by measuring memory
rather than by reading the code and trusting it.

Peak working set is the right figure: current RSS at the end of a run says
nothing about the 4 GB allocation that happened in the middle.
"""

from __future__ import annotations

import os
import sys


def peak_rss_bytes() -> int:
    """Return the highest resident set size this process has reached.

    Windows is the target platform and has no ``resource`` module, so the
    figure comes from ``GetProcessMemoryInfo``; POSIX uses ``getrusage``, whose
    units differ between Linux (kilobytes) and macOS (bytes).
    """
    if os.name == "nt":
        return _windows_peak_working_set()

    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def _windows_peak_working_set() -> int:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    # The signatures are declared rather than left to ctypes' defaults.
    # GetCurrentProcess returns the pseudo-handle (HANDLE)-1; with the default
    # c_int return type that value is truncated on 64-bit and the call fails
    # with "invalid handle".
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.PeakWorkingSetSize)


__all__ = ["peak_rss_bytes"]
