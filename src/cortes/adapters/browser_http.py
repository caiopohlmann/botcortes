"""Cliente HTTP que se apresenta como um navegador comum (YouTube e Kick "como humano").

As páginas e endpoints usados são os mesmos que o site carrega para quem assiste. O ritmo
das consultas fica em escala humana (uma checagem por canal a cada 30-60 s).
"""

from __future__ import annotations

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


def browser_client(
    *, timeout_s: float = 15.0, cookies: dict[str, str] | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        cookies=cookies,
        timeout=timeout_s,
        follow_redirects=True,
    )
