"""Status das lives do YouTube abrindo a página /live do canal, como um espectador faria.

Sem chave de API e sem cota. Cada canal é checado no máximo a cada `check_interval_s`
(padrão 60 s); entre uma checagem e outra o último resultado é reaproveitado.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence

import httpx
import structlog

from cortes.adapters.browser_http import browser_client
from cortes.adapters.youtube.parsing import PageFormatError, parse_live_page
from cortes.domain.models import Channel, LiveStream

log = structlog.get_logger(__name__)

# Evita a tela de consentimento de cookies (comum fora do Brasil).
CONSENT_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}


def youtube_client() -> httpx.AsyncClient:
    return browser_client(cookies=CONSENT_COOKIES)


async def fetch_live(http: httpx.AsyncClient, channel: Channel) -> LiveStream | None:
    resp = await http.get(channel.watch_url)
    if "consent." in resp.url.host:
        raise PageFormatError("YouTube redirecionou para a tela de consentimento")
    if resp.status_code == 404:
        raise LookupError(f"canal do YouTube não existe: {channel.url}")
    resp.raise_for_status()
    return parse_live_page(resp.text, channel.key)


class YouTubeStatusProvider:
    def __init__(
        self, check_interval_s: float = 60.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._http = youtube_client()
        self._interval = check_interval_s
        self._clock = clock
        self._cache: dict[str, tuple[float, LiveStream | None]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_live_streams(
        self, channels: Sequence[Channel]
    ) -> Mapping[str, LiveStream | None]:
        now = self._clock()
        due = [
            c
            for c in channels
            if c.key not in self._cache or now - self._cache[c.key][0] >= self._interval
        ]
        results = await asyncio.gather(
            *(fetch_live(self._http, c) for c in due), return_exceptions=True
        )
        errors: list[BaseException] = []
        for channel, result in zip(due, results, strict=True):
            if isinstance(result, BaseException):
                self._cache.pop(channel.key, None)  # desconhecido: tenta de novo no próximo poll
                errors.append(result)
                log.warning("youtube_status_failed", channel=channel.key, error=repr(result))
            else:
                self._cache[channel.key] = (now, result)

        status = {c.key: self._cache[c.key][1] for c in channels if c.key in self._cache}
        if not status and errors:
            raise errors[0]
        return status
