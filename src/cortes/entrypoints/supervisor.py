"""`cortes run`: sobe monitor, worker e painel como processos separados e os reinicia se caírem.

Processos separados = isolamento real: um travamento do driver/CUDA no worker não derruba
a gravação, um erro no painel não afeta nada, e o GIL de um não atrasa o outro.

Ao encerrar (Ctrl+C), o sinal chega a todos os processos do terminal: os filhos param
sozinhos (o worker devolve o job em andamento para a fila). O supervisor só força o
encerramento de quem não sair dentro do prazo.

O supervisor publica no banco quantas vezes cada processo reiniciou (o painel mostra).
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import structlog

from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.config import Settings
from cortes.entrypoints.status import heartbeat_loop
from cortes.observability import error_counter

log = structlog.get_logger(__name__)

_GRACEFUL_S = 20.0  # tempo para o filho terminar sozinho depois do Ctrl+C


@dataclass(slots=True)
class _ProcessInfo:
    pid: int | None = None
    started_at: float | None = None
    restarts: int = 0
    restart_times: deque[float] = field(default_factory=lambda: deque(maxlen=100))
    last_exit_code: int | None = None
    last_exit_at: float | None = None

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "running": self.pid is not None,
            "started_at": self.started_at,
            "restarts": self.restarts,
            "restarts_1h": sum(1 for t in self.restart_times if t >= now - 3600),
            "last_exit_code": self.last_exit_code,
            "last_exit_at": self.last_exit_at,
        }


async def _wait(proc: asyncio.subprocess.Process, timeout_s: float) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except TimeoutError:
        return False
    return True


async def _shutdown(name: str, proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None or await _wait(proc, _GRACEFUL_S):
        return
    if sys.platform != "win32":
        # POSIX: SIGINT dá ao filho a mesma saída limpa do Ctrl+C.
        proc.send_signal(signal.SIGINT)
        if await _wait(proc, 10.0):
            return
    log.warning("process_force_stop", process=name, pid=proc.pid)
    proc.kill()
    await proc.wait()


async def _keep_alive(name: str, info: _ProcessInfo) -> None:
    backoff = 2.0
    while True:
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "cortes", name)
        except OSError as exc:
            log.error("process_spawn_failed", process=name, error=repr(exc))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue
        info.pid, info.started_at = proc.pid, time.time()
        log.info("process_started", process=name, pid=proc.pid)
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            await asyncio.shield(_shutdown(name, proc))
            raise
        uptime = time.monotonic() - started
        info.pid = None
        info.last_exit_code, info.last_exit_at = code, time.time()
        info.restarts += 1
        info.restart_times.append(time.time())
        log.warning("process_exited", process=name, code=code, uptime_s=round(uptime))
        if uptime > 300:
            backoff = 2.0
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120.0)


async def run_all(settings: Settings) -> None:
    names = ["monitor", "worker"]
    if settings.panel.enabled:
        names.append("panel")
    infos = {name: _ProcessInfo() for name in names}
    store = SqliteStatusStore(settings.paths.database)

    async def collect() -> dict[str, Any]:
        now = time.time()
        return {
            "processes": {name: info.as_dict(now) for name, info in infos.items()},
            "log": error_counter.summary(),
        }

    async with asyncio.TaskGroup() as tg:
        for name in names:
            tg.create_task(_keep_alive(name, infos[name]))
        tg.create_task(
            heartbeat_loop(store, "supervisor", collect, interval_s=settings.panel.heartbeat_s)
        )
