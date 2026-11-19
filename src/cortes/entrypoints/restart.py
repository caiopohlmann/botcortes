"""Botão "Reiniciar sistema" do painel: o pedido fica em `controls` (`restart:system`).

Quem obedece ao pedido:
  - o supervisor (`cortes run`): para monitor, worker e painel e sobe os três de novo, com o
    código e a configuração atuais. Quem não sair sozinho dentro do prazo é encerrado à força;
  - o monitor e o worker: veem o mesmo pedido e saem limpos antes do prazo (o monitor fecha
    gravações e chats; o worker devolve o job em andamento para a fila).

Cada um só obedece a pedidos mais novos que o próprio início, então o registro pode ficar no
banco sem causar reinícios em sequência.

Botão "Pausar tudo" (`system_paused` em `controls`, fica até "Iniciar tudo"): mesmo caminho de
saída. Monitor e worker saem limpos (`PauseRequested`), e o supervisor não os sobe de novo
enquanto a pausa durar. O painel continua no ar (é nele que se retoma).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import structlog

log = structlog.get_logger(__name__)

SYSTEM = "system"  # chave do pedido em `controls` (restart:system)
RESTART_CHECK_S = 2.0  # mesmo ritmo da leitura das pausas


class RestartRequested(Exception):
    """O painel pediu para reiniciar: o processo sai limpo e o supervisor o sobe de novo."""


class PauseRequested(Exception):
    """O painel pausou o sistema: o processo sai limpo e o supervisor espera o "Iniciar tudo"."""


async def new_requests(
    requested_at: Callable[[], float | None],
    after: float,
    interval_s: float = RESTART_CHECK_S,
) -> AsyncIterator[float]:
    """Entrega o horário de cada pedido novo (mais novo que `after` e que o anterior)."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            at = await asyncio.to_thread(requested_at)
        except Exception as exc:  # banco ocupado: tenta na próxima volta
            log.warning("restart_check_failed", error=repr(exc))
            continue
        if at is not None and at > after:
            after = at
            yield at


async def watch_restart(
    requested_at: Callable[[], float | None],
    started_at: float,
    interval_s: float = RESTART_CHECK_S,
) -> None:
    """Levanta RestartRequested quando aparece um pedido mais novo que o início do processo."""
    async for at in new_requests(requested_at, started_at, interval_s):
        log.info("restart_requested", requested_at=at)
        raise RestartRequested


async def watch_pause(
    is_paused: Callable[[], bool],
    interval_s: float = RESTART_CHECK_S,
) -> None:
    """Levanta PauseRequested quando o sistema está pausado (confere já na largada)."""
    while True:
        try:
            paused = await asyncio.to_thread(is_paused)
        except Exception as exc:  # banco ocupado: tenta na próxima volta
            log.warning("pause_check_failed", error=repr(exc))
            paused = False
        if paused:
            log.info("system_pause_requested")
            raise PauseRequested
        await asyncio.sleep(interval_s)
