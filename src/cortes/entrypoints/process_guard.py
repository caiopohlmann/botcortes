"""Windows: os processos filhos (streamlink, FFmpeg, monitor/worker) morrem junto com o pai.

No Windows, matar um processo NÃO mata os filhos dele. Se o monitor travar e for
encerrado à força (pelo supervisor, pelo Gerenciador de Tarefas ou por um crash), o
streamlink e o FFmpeg continuariam rodando escondidos, baixando a live para sempre.

Solução padrão do Windows: um "Job Object" com KILL_ON_JOB_CLOSE. O processo entra no
job, os filhos herdam, e quando o processo termina (de qualquer jeito) o sistema fecha
o job e encerra todos os que estão nele. Cada processo do bot cria o seu próprio job
(jobs aninhados funcionam desde o Windows 8).
"""

from __future__ import annotations

import sys

_job_handle: int | None = None  # mantido aberto até o processo terminar


def kill_children_on_exit() -> str | None:
    """Ativa a proteção. Retorna None se ok, ou a descrição do erro (não é fatal)."""
    global _job_handle
    if sys.platform != "win32":  # verificação isolada: o mypy pula o resto fora do Windows
        return None
    if _job_handle is not None:
        return None

    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = (  # layout fixo da struct do Windows (x64)
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        )

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x2000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return f"CreateJobObject falhou (erro {ctypes.get_last_error()})"
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    ok = kernel32.SetInformationJobObject(
        job, job_object_extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        return f"SetInformationJobObject falhou (erro {error})"
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        return f"AssignProcessToJobObject falhou (erro {error})"
    _job_handle = int(job)
    return None
