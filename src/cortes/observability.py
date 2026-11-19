"""Logs estruturados (console legível + arquivo JSON rotativo), contagem de erros e
coleta de uso do PC (CPU, RAM, disco, rede, GPU) para o painel."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class ErrorCounter(logging.Handler):
    """Conta avisos/erros da última hora do processo (vai no batimento para o painel)."""

    def __init__(self, window_s: float = 3600.0, keep_last: int = 8) -> None:
        super().__init__(level=logging.WARNING)
        self._window = window_s
        self._events: deque[tuple[float, int]] = deque()
        self._last: deque[dict[str, Any]] = deque(maxlen=keep_last)
        self._lock_counts = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        event = record.msg.get("event") if isinstance(record.msg, dict) else None
        if not isinstance(event, str):
            try:
                event = record.getMessage()
            except Exception:
                event = str(record.msg)
        detail = record.msg.get("error") if isinstance(record.msg, dict) else None
        with self._lock_counts:
            self._events.append((record.created, record.levelno))
            self._last.append(
                {
                    "ts": record.created,
                    "level": record.levelname.lower(),
                    "event": event[:200],
                    "detail": str(detail)[:300] if detail else None,
                    "logger": record.name,
                }
            )

    def summary(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock_counts:
            while self._events and self._events[0][0] < now - self._window:
                self._events.popleft()
            warnings = sum(1 for _, level in self._events if level < logging.ERROR)
            errors = len(self._events) - warnings
            last = list(self._last)
        return {"warnings_1h": warnings, "errors_1h": errors, "last": last[::-1]}


error_counter = ErrorCounter()


def configure_logging(level: str, log_file: Path | None = None) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            ],
            foreign_pre_chain=shared,
        )
    )
    handlers: list[logging.Handler] = [console]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(ensure_ascii=False),
                ],
                foreign_pre_chain=shared,
            )
        )
        handlers.append(file_handler)

    handlers.append(error_counter)

    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(level.upper())
    # Bibliotecas barulhentas
    for noisy in ("httpx", "httpcore", "websockets", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_GPU_FIELDS = (
    ("name", "name"),
    ("util_pct", "utilization.gpu"),
    ("encoder_pct", "utilization.encoder"),
    ("vram_used_mb", "memory.used"),
    ("vram_total_mb", "memory.total"),
    ("temp_c", "temperature.gpu"),
    ("power_w", "power.draw"),
)


def _number(value: str) -> float | str | None:
    value = value.strip()
    if not value or value.startswith("[") or value.upper() == "N/A":
        return None  # "[Not Supported]" em algumas placas
    try:
        return float(value)
    except ValueError:
        return value


async def query_gpu() -> dict[str, Any] | None:
    """Uma leitura do nvidia-smi (primeira GPU). None se não houver NVIDIA/driver."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    query = ",".join(field for _, field in _GPU_FIELDS)
    proc = await asyncio.create_subprocess_exec(
        exe,
        f"--query-gpu={query}",
        "--format=csv,noheader,nounits",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    lines = out.decode(errors="replace").strip().splitlines()
    if proc.returncode != 0 or not lines:
        return None
    values = [v.strip() for v in lines[0].split(",")]
    if len(values) != len(_GPU_FIELDS):
        return None
    return {
        key: (raw if key == "name" else _number(raw))
        for (key, _), raw in zip(_GPU_FIELDS, values, strict=True)
    }


async def gpu_stats_loop(interval_s: float) -> None:
    """Registra no log a utilização e a memória da GPU (se houver NVIDIA)."""
    if interval_s <= 0:
        return
    while True:
        try:
            stats = await query_gpu()
            if stats is not None:
                log.info("gpu_stats", **stats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("gpu_stats_failed", error=repr(exc))
        await asyncio.sleep(interval_s)


class HostSampler:
    """Uso do PC: CPU, RAM, disco, rede e os processos filhos (streamlink, FFmpeg).

    `sample()` mede a CPU e a rede desde a chamada anterior, então deve ser chamado
    em intervalos regulares (o batimento faz isso a cada poucos segundos).
    """

    def __init__(self, disk_path: Path) -> None:
        import psutil

        self._psutil = psutil
        self._disk_path = disk_path
        self._proc = psutil.Process(os.getpid())
        self._last_net: tuple[float, int, int] | None = None
        psutil.cpu_percent(interval=None)  # primeira leitura serve só de referência

    def sample(self) -> dict[str, Any]:
        ps = self._psutil
        now = time.time()
        memory = ps.virtual_memory()
        disk = shutil.disk_usage(self._disk_path)
        net = ps.net_io_counters()
        rx_mbps = tx_mbps = None
        if self._last_net is not None:
            elapsed = max(now - self._last_net[0], 1e-6)
            rx_mbps = (net.bytes_recv - self._last_net[1]) * 8 / elapsed / 1e6
            tx_mbps = (net.bytes_sent - self._last_net[2]) * 8 / elapsed / 1e6
        self._last_net = (now, net.bytes_recv, net.bytes_sent)

        children: list[Any] = []
        with contextlib.suppress(ps.Error):
            children = self._proc.children(recursive=True)
        child_rss = 0
        for child in children:
            try:
                child_rss += child.memory_info().rss
            except ps.Error:
                continue
        return {
            "cpu_pct": ps.cpu_percent(interval=None),
            "cpu_count": ps.cpu_count(),
            "ram_used_gb": (memory.total - memory.available) / 1024**3,
            "ram_total_gb": memory.total / 1024**3,
            "ram_pct": memory.percent,
            "disk_free_gb": disk.free / 1024**3,
            "disk_total_gb": disk.total / 1024**3,
            "net_rx_mbps": rx_mbps,
            "net_tx_mbps": tx_mbps,
            "boot_time": ps.boot_time(),
            "process_rss_mb": self._proc.memory_info().rss / 1024**2,
            "children": len(children),
            "children_rss_mb": child_rss / 1024**2,
        }
