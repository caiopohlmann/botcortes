"""Status das lives via Twitch Helix (token de app, client credentials).

Uma única chamada GET /streams cobre até 100 canais: polling a cada 30 s é barato e não
precisa de URL pública (webhook) nem de token de usuário (EventSub via WebSocket).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import httpx
import structlog

from cortes.domain.models import Channel, LiveStream

log = structlog.get_logger(__name__)

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_API = "https://api.twitch.tv/helix"
_BATCH = 100


class TwitchAuthError(RuntimeError):
    pass


class HelixClient:
    def __init__(self, client_id: str, client_secret: str, *, timeout_s: float = 15.0) -> None:
        if not client_id or not client_secret:
            raise TwitchAuthError("Defina TWITCH_CLIENT_ID e TWITCH_CLIENT_SECRET no .env")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(timeout=timeout_s)
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 300:
            return self._token
        resp = await self._http.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
        )
        if resp.status_code in (400, 401, 403):
            raise TwitchAuthError(f"Credenciais da Twitch recusadas: {resp.text}")
        resp.raise_for_status()
        body = resp.json()
        self._token = str(body["access_token"])
        self._token_expires_at = time.time() + float(body.get("expires_in", 3600))
        log.info("twitch_token_ok", expires_in=body.get("expires_in"))
        return self._token

    async def _get(self, path: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        for attempt in range(2):
            token = await self._ensure_token()
            resp = await self._http.get(
                f"{_API}{path}",
                params=tuple(params),
                headers={"Client-Id": self._client_id, "Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401 and attempt == 0:
                self._token = None  # token revogado/expirado: renova e tenta de novo
                continue
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
        raise TwitchAuthError("Token da Twitch inválido mesmo após renovação")

    async def get_display_name(self, login: str) -> str | None:
        """Nome do canal como a Twitch mostra (None = o canal não existe)."""
        body = await self._get("/users", [("login", login)])
        users = body.get("data") or []
        return str(users[0].get("display_name") or login) if users else None

    async def get_live_streams(
        self, channels: Sequence[Channel]
    ) -> Mapping[str, LiveStream | None]:
        keys = {c.login.casefold(): c.key for c in channels}
        status: dict[str, LiveStream | None] = dict.fromkeys(keys.values())
        logins = list(keys)
        for i in range(0, len(logins), _BATCH):
            batch = logins[i : i + _BATCH]
            params = [("user_login", login) for login in batch]
            params.append(("first", str(_BATCH)))
            body = await self._get("/streams", params)
            for item in body.get("data", []):
                key = keys.get(str(item.get("user_login", "")).casefold())
                if key is None or item.get("type") != "live":
                    continue
                status[key] = LiveStream(
                    key=key,
                    user_name=str(item.get("user_name") or item.get("user_login")),
                    title=str(item.get("title") or ""),
                    game_name=str(item.get("game_name") or ""),
                    started_at=_parse_time(item.get("started_at")),
                    viewer_count=int(item.get("viewer_count") or 0),
                )
        return status


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
