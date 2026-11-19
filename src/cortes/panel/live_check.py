"""Botão "Verificar ao vivo agora": o painel consulta as plataformas por conta própria.

Serve com o sistema pausado (monitor desligado) ou para não esperar o próximo ciclo de
30 s do monitor. Usa os mesmos provedores do monitor (uma consulta por plataforma, no
ritmo de um espectador). Não grava nada e não liga o monitor: só informa.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from cortes.config import Secrets, Settings
from cortes.domain.models import Channel, Platform
from cortes.entrypoints.status import stream_dict

log = structlog.get_logger(__name__)


@dataclass
class LiveCheck:
    """Resultado da última verificação (fica na memória do painel)."""

    checked_at: float = 0.0
    # Channel.key -> info da live (stream_dict) ou None (offline). Só quem respondeu.
    results: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)  # canais sem resposta
    message: str = ""

    def as_rows(self) -> dict[str, dict[str, Any]]:
        """Formato que panel.views.channel_rows usa."""
        return {
            key: {"checked_at": self.checked_at, "stream": stream}
            for key, stream in self.results.items()
        }


async def check_live(
    channels: Sequence[Channel], settings: Settings, secrets: Secrets
) -> LiveCheck:
    from cortes.entrypoints.monitor import build_status_providers

    groups: dict[Platform, list[Channel]] = {}
    for channel in channels:
        groups.setdefault(channel.platform, []).append(channel)
    check = LiveCheck(checked_at=time.time())

    async def one(platform: Platform, members: list[Channel]) -> None:
        only = settings.model_copy(update={"channels": members})
        try:
            providers, closers = build_status_providers(only, secrets)
        except Exception as exc:  # ex.: Twitch sem credenciais no .env
            log.warning("live_check_failed", platform=platform.value, error=repr(exc))
            check.failed += [c.key for c in members]
            return
        try:
            status = await providers[platform].get_live_streams(members)
        except Exception as exc:
            log.warning("live_check_failed", platform=platform.value, error=repr(exc))
            status = {}
        finally:
            for close in closers:
                with contextlib.suppress(Exception):
                    await close()
        for c in members:
            if c.key in status:
                check.results[c.key] = stream_dict(status[c.key])
            else:
                check.failed.append(c.key)

    await asyncio.gather(*(one(p, members) for p, members in groups.items()))
    names = {c.key: c.name for c in channels}
    live = [names[k] for k in names if check.results.get(k) is not None]
    message = f"Ao vivo agora: {', '.join(live)}" if live else "Ninguém ao vivo agora"
    message += f" ({len(live)} de {len(channels)})."
    if check.failed:
        message += f" Sem resposta: {', '.join(names[k] for k in check.failed)}."
    check.message = message
    log.info("live_checked_by_panel", live=len(live), failed=check.failed)
    return check
