"""`cortes run`: sobe monitor, worker e painel como processos separados e os reinicia se caírem.

Processos separados = isolamento real: um travamento do driver/CUDA no worker não derruba
a gravação, um erro no painel não afeta nada, e o GIL de um não atrasa o outro.

Ao encerrar (Ctrl+C), o sinal chega a todos os processos do terminal: os filhos param
sozinhos (o worker devolve o job em andamento para a fila). O supervisor só força o
encerramento de quem não sair dentro do prazo.

O supervisor publica no banco quantas vezes cada processo reiniciou (o painel mostra).

Botão "Reiniciar sistema" do painel (ver entrypoints/restart.py): o supervisor para monitor,
worker e painel e sobe os três de novo, com o código e a configuração atuais. Monitor e worker
veem o mesmo pedido e saem limpos; quem não sair em _RESTART_GRACE_S (travado, por exemplo) é
encerrado à força. O painel não tem nada a salvar e é encerrado na hora. Esses reinícios
pedidos não contam como queda no painel. O próprio supervisor não reinicia: uma mudança em
supervisor.py só vale depois de fechar e abrir o `cortes run`.

Botão "Pausar tudo" (`system_paused`): o supervisor para monitor e worker do mesmo jeito e não
os sobe de novo até o "Iniciar tudo". O painel continua no ar. A pausa fica no banco: abrir o
`cortes run` com o sistema pausado deixa monitor e worker desligados até alguém retomar.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.config import Settings
from cortes.entrypoints.restart import RESTART_CHECK_S, SYSTEM, new_requests
from cortes.entrypoints.status import heartbeat_loop
from cortes.observability import error_counter

log = structlog.get_logger(__name__)

_GRACEFUL_S = 20.0  # tempo para o filho terminar sozinho depois do Ctrl+C
_RESTART_GRACE_S = 30.0  # "Reiniciar sistema": prazo para monitor/worker saírem sozinhos
_SELF_EXIT = frozenset({"monitor", "worker"})  # obedecem a reinício e pausa sozinhos
_PAUSABLE = _SELF_EXIT  # desligados no "Pausar tudo" (o painel fica para poder retomar)
_PAUSE_CHECK_S = RESTART_CHECK_S
_RESPAWN_PAUSE_S = 1.0  # depois de um reinício pedido (dá tempo de liberar a porta do painel)


@dataclass(slots=True)
class _ProcessInfo:
    pid: int | None = None
    started_at: float | None = None
    restarts: int = 0
    restart_times: deque[float] = field(default_factory=lambda: deque(maxlen=100))
    last_exit_code: int | None = None
    last_exit_at: float | None = None
    restart_at: float | None = None  # último pedido de "Reiniciar sistema" visto
    paused: bool = False  # "Pausar tudo": fica desligado até "Iniciar tudo"
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # avisa que chegou um pedido

    def restart_pending(self) -> bool:
        """Há pedido de reinício mais novo que o processo em execução?"""
        return (
            self.restart_at is not None
            and self.started_at is not None
            and self.restart_at > self.started_at
        )

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "running": self.pid is not None,
            "paused": self.paused,
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


async def _stop_for_restart(
    name: str, proc: asyncio.subprocess.Process, reason: str = "restart"
) -> None:
    """Espera monitor/worker saírem sozinhos; o painel (ou quem passar do prazo) é encerrado."""
    if name in _SELF_EXIT and await _wait(proc, _RESTART_GRACE_S):
        return
    if proc.returncode is not None:
        return
    if name in _SELF_EXIT:
        log.warning("process_force_stop", process=name, pid=proc.pid, reason=reason)
    else:
        log.info("process_stopped_for_restart", process=name, pid=proc.pid)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


async def _wait_exit_or_restart(
    name: str, proc: asyncio.subprocess.Process, info: _ProcessInfo
) -> int:
    """Espera o processo sair; se chegar um pedido de reinício ou pausa antes, para o processo."""
    exited = asyncio.ensure_future(proc.wait())
    try:
        while not exited.done():
            info.wake.clear()
            if info.paused:
                await _stop_for_restart(name, proc, reason="pause")
                break
            if info.restart_pending():
                await _stop_for_restart(name, proc)
                break
            woke = asyncio.ensure_future(info.wake.wait())
            try:
                await asyncio.wait({exited, woke}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                woke.cancel()
        return await exited
    finally:
        exited.cancel()  # sem efeito se já terminou


async def _requested_since(store: SqliteStatusStore, since: float) -> bool:
    """O processo saiu por um "Reiniciar sistema" (ele viu o pedido antes do supervisor)?"""
    try:
        at = await asyncio.to_thread(store.restart_requested_at, SYSTEM)
    except Exception:
        return False
    return at is not None and at > since


async def _system_paused(store: SqliteStatusStore) -> bool | None:
    """Estado do "Pausar tudo" no banco; None se não deu para ler (banco ocupado)."""
    try:
        controls = await asyncio.to_thread(store.get_controls)
    except Exception as exc:
        log.warning("pause_check_failed", error=repr(exc))
        return None
    return controls.system_paused


async def _hold_while_paused(name: str, info: _ProcessInfo) -> None:
    """Não sobe o processo enquanto o sistema estiver pausado (espera o "Iniciar tudo")."""
    if not info.paused:
        return
    log.info("process_held_paused", process=name)
    while info.paused:
        info.wake.clear()
        await info.wake.wait()
    log.info("process_resuming", process=name)


async def _keep_alive(
    name: str,
    info: _ProcessInfo,
    store: SqliteStatusStore,
    argv: Sequence[str] | None = None,
) -> None:
    cmd = list(argv) if argv else [sys.executable, "-m", "cortes", name]
    backoff = 2.0
    while True:
        await _hold_while_paused(name, info)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(*cmd)
        except OSError as exc:
            log.error("process_spawn_failed", process=name, error=repr(exc))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue
        info.pid, info.started_at = proc.pid, time.time()
        log.info("process_started", process=name, pid=proc.pid)
        try:
            code = await _wait_exit_or_restart(name, proc, info)
        except asyncio.CancelledError:
            await asyncio.shield(_shutdown(name, proc))
            raise
        uptime = time.monotonic() - started
        # Tudo que consulta o banco vem antes de registrar a saída (o painel lê o resultado).
        if name in _PAUSABLE and not info.paused and await _system_paused(store):
            info.paused = True  # saiu sozinho pela pausa antes de o supervisor ver o pedido
        planned = info.paused or info.restart_pending()
        if not planned:
            planned = await _requested_since(store, info.started_at)
        info.pid = None
        info.last_exit_code, info.last_exit_at = code, time.time()
        if info.paused:
            # "Pausar tudo": não conta como queda; volta no "Iniciar tudo".
            log.info("process_paused", process=name, code=code, uptime_s=round(uptime))
            backoff = 2.0
            continue
        if planned:
            # Reinício pedido pelo painel: não conta como queda e volta sem espera crescente.
            log.info("process_restarted", process=name, code=code, uptime_s=round(uptime))
            backoff = 2.0
            await asyncio.sleep(_RESPAWN_PAUSE_S)
            continue
        info.restarts += 1
        info.restart_times.append(time.time())
        log.warning("process_exited", process=name, code=code, uptime_s=round(uptime))
        if uptime > 300:
            backoff = 2.0
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120.0)


async def _watch_system_restart(
    store: SqliteStatusStore,
    infos: Mapping[str, _ProcessInfo],
    started_at: float,
    interval_s: float = RESTART_CHECK_S,
) -> None:
    """Repassa cada "Reiniciar sistema" do painel para os processos supervisionados."""
    requested_at = lambda: store.restart_requested_at(SYSTEM)  # noqa: E731
    async for at in new_requests(requested_at, started_at, interval_s):
        log.info("system_restart_requested", requested_at=at)
        for info in infos.values():
            info.restart_at = at
            info.wake.set()


def _apply_pause(infos: Mapping[str, _ProcessInfo], paused: bool) -> bool:
    """Marca monitor/worker como pausados (ou não). Devolve True se algo mudou."""
    changed = False
    for name, info in infos.items():
        if name in _PAUSABLE and info.paused != paused:
            info.paused = paused
            info.wake.set()
            changed = True
    return changed


async def _watch_system_pause(
    store: SqliteStatusStore,
    infos: Mapping[str, _ProcessInfo],
    interval_s: float = _PAUSE_CHECK_S,
) -> None:
    """Repassa o "Pausar tudo"/"Iniciar tudo" do painel para monitor e worker."""
    while True:
        await asyncio.sleep(interval_s)
        paused = await _system_paused(store)
        if paused is None:  # banco ocupado: mantém o estado atual
            continue
        if _apply_pause(infos, paused):
            log.info("system_paused" if paused else "system_resumed")


async def run_all(settings: Settings) -> None:
    names = ["monitor", "worker"]
    if settings.panel.enabled:
        names.append("panel")
    infos = {name: _ProcessInfo() for name in names}
    store = SqliteStatusStore(settings.paths.database)
    started_at = time.time()
    if await _system_paused(store):
        _apply_pause(infos, True)
        log.info("system_paused_at_start", hint="clique em 'Iniciar tudo' no painel")

    async def collect() -> dict[str, Any]:
        now = time.time()
        return {
            "processes": {name: info.as_dict(now) for name, info in infos.items()},
            # Também avisa o painel que este supervisor sabe obedecer ao "Pausar tudo".
            "system_paused": any(infos[name].paused for name in names if name in _PAUSABLE),
            "log": error_counter.summary(),
        }

    async with asyncio.TaskGroup() as tg:
        for name in names:
            tg.create_task(_keep_alive(name, infos[name], store))
        tg.create_task(
            heartbeat_loop(store, "supervisor", collect, interval_s=settings.panel.heartbeat_s)
        )
        tg.create_task(_watch_system_restart(store, infos, started_at))
        tg.create_task(_watch_system_pause(store, infos))
