"""Status das lives do Kick.

Caminho principal ("como humano"): kick.com/api/v2/channels/<slug>, o mesmo JSON que a
página do canal carrega. Não precisa de conta e ainda informa o id do chatroom (chat).

Esse endpoint fica atrás do Cloudflare, que às vezes bloqueia clientes que não são
navegador (HTTP 403). Se KICK_CLIENT_ID/SECRET estiverem no .env, os canais bloqueados são
consultados pela API oficial (api.kick.com, token de app). A API oficial não informa o
chatroom; nesse caso o chat usa `chat_id` do channels.yaml (ver README).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence

import httpx
import structlog

from cortes.adapters.browser_http import browser_client
from cortes.adapters.kick.parsing import parse_official_channels, parse_public_channel
from cortes.domain.models import Channel, LiveStream

log = structlog.get_logger(__name__)

_PUBLIC_API = "https://kick.com/api/v2/channels/{slug}"
_TOKEN_URL = "https://id.kick.com/oauth/token"
_OFFICIAL_API = "https://api.kick.com/public/v1/channels"
_OFFICIAL_BATCH = 50


class KickBlockedError(RuntimeError):
    """O Cloudflare do Kick recusou a requisição (HTTP 403/429)."""


async def fetch_public_channel(
    http: httpx.AsyncClient, slug: str, key: str
) -> tuple[LiveStream | None, str | None]:
    resp = await http.get(
        _PUBLIC_API.format(slug=slug),
        headers={"Accept": "application/json", "Referer": f"https://kick.com/{slug}"},
    )
    if resp.status_code in (403, 429, 503):
        raise KickBlockedError(f"kick.com respondeu {resp.status_code} para {slug}")
    if resp.status_code == 404:
        raise LookupError(f"canal do Kick não existe: {slug}")
    resp.raise_for_status()
    return parse_public_channel(resp.json(), key)


class KickOfficialClient:
    def __init__(self, client_id: str, client_secret: str, *, timeout_s: float = 15.0) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(timeout=timeout_s)
        self._token: str | None = None
        self._expires_at = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._expires_at - 300:
            return self._token
        resp = await self._http.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        if resp.status_code in (400, 401, 403):
            raise RuntimeError(f"credenciais do Kick recusadas: {resp.text[:200]}")
        resp.raise_for_status()
        body = resp.json()
        self._token = str(body["access_token"])
        self._expires_at = time.time() + float(body.get("expires_in", 3600))
        return self._token

    async def get_channels(self, channels: Sequence[Channel]) -> dict[str, LiveStream | None]:
        keys = {c.login: c.key for c in channels}
        slugs = list(keys)
        status: dict[str, LiveStream | None] = {}
        for i in range(0, len(slugs), _OFFICIAL_BATCH):
            params = [("slug", slug) for slug in slugs[i : i + _OFFICIAL_BATCH]]
            for attempt in range(2):
                token = await self._ensure_token()
                resp = await self._http.get(
                    _OFFICIAL_API, params=params, headers={"Authorization": f"Bearer {token}"}
                )
                if resp.status_code == 401 and attempt == 0:
                    self._token = None
                    continue
                resp.raise_for_status()
                status.update(parse_official_channels(resp.json(), keys))
                break
        return status


class KickStatusProvider:
    def __init__(self, official: KickOfficialClient | None = None) -> None:
        self._http = browser_client()
        self._official = official
        self._blocked_logged = False

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._official is not None:
            await self._official.aclose()

    async def get_live_streams(
        self, channels: Sequence[Channel]
    ) -> Mapping[str, LiveStream | None]:
        results = await asyncio.gather(
            *(fetch_public_channel(self._http, c.login, c.key) for c in channels),
            return_exceptions=True,
        )
        status: dict[str, LiveStream | None] = {}
        blocked: list[Channel] = []
        errors: list[BaseException] = []
        for channel, result in zip(channels, results, strict=True):
            if isinstance(result, KickBlockedError):
                blocked.append(channel)
            elif isinstance(result, BaseException):
                errors.append(result)
                log.warning("kick_status_failed", channel=channel.key, error=repr(result))
            else:
                status[channel.key] = result[0]

        if blocked:
            if self._official is None:
                if not self._blocked_logged:
                    self._blocked_logged = True
                    log.warning(
                        "kick_blocked",
                        channels=[c.key for c in blocked],
                        hint="defina KICK_CLIENT_ID/KICK_CLIENT_SECRET no .env (API oficial)",
                    )
                errors.append(KickBlockedError("bloqueado pelo Cloudflare"))
            else:
                status.update(await self._official.get_channels(blocked))

        if not status and errors:
            raise errors[0]  # nada verificado: o LiveMonitor faz backoff da plataforma
        return status
