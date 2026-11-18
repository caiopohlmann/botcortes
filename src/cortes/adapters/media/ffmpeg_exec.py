"""Execução de processos FFmpeg com timeout, cancelamento seguro e erro legível."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path


class FfmpegError(RuntimeError):
    def __init__(self, returncode: int | None, stderr: str) -> None:
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        super().__init__(f"ffmpeg falhou (código {returncode}):\n{tail}")
        self.returncode = returncode
        self.stderr = stderr


async def run_ffmpeg(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_s: float = 600.0,
    capture_stdout: bool = False,
) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except BaseException:
        # Timeout ou cancelamento: não deixa FFmpeg órfão consumindo GPU/CPU.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode != 0:
        raise FfmpegError(proc.returncode, stderr.decode("utf-8", "replace"))
    return stdout or b""
