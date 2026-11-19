"""Botão "Reiniciar sistema": quem obedece e o que o supervisor faz com cada processo."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.entrypoints import supervisor
from cortes.entrypoints.restart import (
    SYSTEM,
    PauseRequested,
    RestartRequested,
    new_requests,
    watch_pause,
    watch_restart,
)

_HANGS = [sys.executable, "-c", "import time; time.sleep(60)"]  # não sai sozinho (travado)


def test_restart_requested_after_start_stops_the_watch() -> None:
    answers = iter([None, 50.0, 150.0])  # nada, pedido antigo, pedido novo

    async def main() -> None:
        await asyncio.wait_for(watch_restart(lambda: next(answers), 100.0, 0.001), 2)

    with pytest.raises(RestartRequested):
        asyncio.run(main())


def test_old_request_and_db_errors_are_ignored() -> None:
    calls = 0

    def requested_at() -> float | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database is locked")
        return 50.0  # pedido de antes do início: já foi atendido

    async def main() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(watch_restart(requested_at, 100.0, 0.001), 0.1)

    asyncio.run(main())
    assert calls > 2


def test_new_requests_delivers_each_new_request_once() -> None:
    answers = iter([150.0, 150.0, 120.0, 200.0])  # repetido e mais velho não contam

    async def main() -> list[float]:
        found: list[float] = []
        async for at in new_requests(lambda: next(answers), 100.0, 0.001):
            found.append(at)
            if len(found) == 2:
                break
        return found

    assert asyncio.run(main()) == [150.0, 200.0]


# ---------------------------------------------------------------------- supervisor
@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "_GRACEFUL_S", 0.2)
    monkeypatch.setattr(supervisor, "_RESTART_GRACE_S", 0.3)
    monkeypatch.setattr(supervisor, "_RESPAWN_PAUSE_S", 0.0)


async def _until(check: Callable[[], bool], timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condição não aconteceu a tempo")
        await asyncio.sleep(0.02)


def _supervise(
    name: str,
    argv: list[str],
    store: SqliteStatusStore,
    scenario: Callable[[supervisor._ProcessInfo], object],
) -> supervisor._ProcessInfo:
    """Roda o _keep_alive de verdade (subprocesso real) durante o `scenario`."""
    info = supervisor._ProcessInfo()

    async def main() -> None:
        task = asyncio.create_task(supervisor._keep_alive(name, info, store, argv))
        try:
            result = scenario(info)
            if asyncio.iscoroutine(result):
                await result
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(main())
    return info


@pytest.mark.parametrize("name", ["panel", "worker"])
def test_restart_stops_a_stuck_process_and_starts_it_again(
    tmp_path: Path, fast: None, name: str
) -> None:
    # painel: encerrado na hora; worker travado: encerrado à força depois do prazo.
    store = SqliteStatusStore(tmp_path / "cortes.db")
    pids: list[int | None] = []

    async def scenario(info: supervisor._ProcessInfo) -> None:
        await _until(lambda: info.pid is not None)
        pids.append(info.pid)
        # +1 s: no Windows o relógio anda de ~15 ms em ~15 ms e time.time() pode repetir o
        # started_at, e um pedido "do mesmo instante" do início é ignorado.
        info.restart_at = time.time() + 1.0
        info.wake.set()
        await _until(lambda: info.pid is not None and info.pid != pids[0])
        pids.append(info.pid)

    info = _supervise(name, _HANGS, store, scenario)
    assert pids[0] != pids[1]
    assert info.restarts == 0  # pedido pelo painel não conta como queda
    assert info.last_exit_at is not None


def test_process_that_left_by_itself_after_the_request_is_not_a_crash(
    tmp_path: Path, fast: None
) -> None:
    # O monitor/worker viu o pedido antes do supervisor e saiu sozinho (código 0).
    store = SqliteStatusStore(tmp_path / "cortes.db")
    store.request_restart(SYSTEM, time.time() + 3600)

    async def scenario(info: supervisor._ProcessInfo) -> None:
        await _until(lambda: info.last_exit_at is not None)

    info = _supervise("monitor", [sys.executable, "-c", "pass"], store, scenario)
    assert info.restarts == 0 and info.last_exit_code == 0


def test_crash_without_request_still_counts(tmp_path: Path, fast: None) -> None:
    store = SqliteStatusStore(tmp_path / "cortes.db")

    async def scenario(info: supervisor._ProcessInfo) -> None:
        await _until(lambda: info.last_exit_at is not None)

    info = _supervise("worker", [sys.executable, "-c", "raise SystemExit(3)"], store, scenario)
    assert info.restarts == 1 and info.last_exit_code == 3


def test_supervisor_passes_the_panel_request_to_every_process(tmp_path: Path) -> None:
    store = SqliteStatusStore(tmp_path / "cortes.db")
    infos = {name: supervisor._ProcessInfo() for name in ("monitor", "worker", "panel")}
    started = time.time()
    store.request_restart(SYSTEM, started - 10)  # de antes do supervisor: ignorado

    async def main() -> None:
        watch = asyncio.create_task(
            supervisor._watch_system_restart(store, infos, started, interval_s=0.01)
        )
        await asyncio.sleep(0.1)
        assert all(info.restart_at is None for info in infos.values())
        store.request_restart(SYSTEM, started + 5)
        await _until(lambda: all(info.wake.is_set() for info in infos.values()))
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    asyncio.run(main())
    assert {info.restart_at for info in infos.values()} == {started + 5}


# ---------------------------------------------------------------------- "Pausar tudo"
def test_watch_pause_stops_when_paused_and_survives_db_errors() -> None:
    answers = iter([False, RuntimeError("database is locked"), True])

    def is_paused() -> bool:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def main() -> None:
        await asyncio.wait_for(watch_pause(is_paused, 0.001), 2)

    with pytest.raises(PauseRequested):
        asyncio.run(main())


def test_pause_stops_the_process_and_resume_starts_it_again(tmp_path: Path, fast: None) -> None:
    store = SqliteStatusStore(tmp_path / "cortes.db")
    pids: list[int | None] = []

    async def scenario(info: supervisor._ProcessInfo) -> None:
        await _until(lambda: info.pid is not None)
        pids.append(info.pid)
        info.paused = True  # o que o _watch_system_pause faz ao ver a pausa
        info.wake.set()
        await _until(lambda: info.pid is None)
        await asyncio.sleep(0.3)
        assert info.pid is None  # não sobe de novo enquanto pausado
        info.paused = False
        info.wake.set()
        await _until(lambda: info.pid is not None)
        pids.append(info.pid)

    info = _supervise("worker", _HANGS, store, scenario)  # travado: encerrado à força
    assert pids[0] != pids[1]
    assert info.restarts == 0  # pausa não conta como queda


def test_process_that_left_by_itself_for_the_pause_is_held(tmp_path: Path, fast: None) -> None:
    # O monitor viu a pausa antes do supervisor e saiu sozinho (código 0).
    store = SqliteStatusStore(tmp_path / "cortes.db")
    store.set_system_paused(True)

    async def scenario(info: supervisor._ProcessInfo) -> None:
        await _until(lambda: info.last_exit_at is not None)
        await asyncio.sleep(0.3)

    info = _supervise("monitor", [sys.executable, "-c", "pass"], store, scenario)
    assert info.paused and info.pid is None
    assert info.restarts == 0 and info.last_exit_code == 0


def test_pause_watch_only_touches_monitor_and_worker(tmp_path: Path) -> None:
    store = SqliteStatusStore(tmp_path / "cortes.db")
    infos = {name: supervisor._ProcessInfo() for name in ("monitor", "worker", "panel")}

    async def main() -> None:
        watch = asyncio.create_task(supervisor._watch_system_pause(store, infos, 0.01))
        store.set_system_paused(True)
        await _until(lambda: infos["monitor"].paused and infos["worker"].paused)
        assert not infos["panel"].paused  # o painel fica no ar para retomar
        store.resume_all()
        await _until(lambda: not infos["monitor"].paused and not infos["worker"].paused)
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    asyncio.run(main())
