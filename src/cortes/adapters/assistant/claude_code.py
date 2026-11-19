"""Chama o Claude Code em modo não interativo (`claude -p`) para ajustar um corte.

Não precisa de terminal aberto nem de alguém aprovando na tela: o painel roda o
`claude` em segundo plano, na pasta do projeto, com permissões limitadas:
  - pode ler/editar arquivos do projeto (edições aceitas automaticamente);
  - no terminal, só pode rodar `uv run cortes rerender`, `ffmpeg` e `ffprobe`;
  - sem acesso à web.
Comandos fora dessa lista são recusados automaticamente (o Claude explica na resposta).

O pedido vai pelo stdin (nunca pela linha de comando): no Windows o `claude` costuma ser
um `claude.cmd`, e texto livre em argumentos de .cmd pode virar comando.

A resposta JSON traz o `session_id`: com ele dá para continuar a mesma conversa no
terminal (`claude --resume <id>`), inclusive pelo botão do painel.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import structlog

from cortes.config import AssistantSettings

log = structlog.get_logger(__name__)

_SESSION_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


@dataclass(frozen=True, slots=True)
class ClaudeResult:
    ok: bool
    reply: str
    session_id: str | None
    cost_usd: float | None
    turns: int | None
    error: str | None = None


class ClaudeNotFoundError(RuntimeError):
    pass


def find_claude(settings: AssistantSettings) -> str:
    exe = shutil.which(settings.claude_path)
    if exe is None:
        raise ClaudeNotFoundError(
            f"'{settings.claude_path}' não encontrado no PATH. Instale o Claude Code neste PC "
            "e faça login uma vez no terminal (`claude`), ou ajuste assistant.claude_path."
        )
    return exe


def build_args(exe: str, settings: AssistantSettings) -> list[str]:
    allowed = ["Read", "Glob", "Grep", "Edit", "Write", "Bash(uv run cortes rerender:*)"]
    if settings.allow_ffmpeg:
        allowed += ["Bash(ffmpeg:*)", "Bash(ffprobe:*)"]
    args = [
        exe,
        "-p",
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--allowedTools", *allowed,
        "--disallowedTools", "WebFetch", "WebSearch",
        "--max-turns", str(settings.max_turns),
    ]  # fmt: skip
    if settings.model:
        args += ["--model", settings.model]
    return args


def parse_result(stdout: str, returncode: int | None, stderr: str) -> ClaudeResult:
    """Lê a saída `--output-format json` (um objeto com result, session_id, is_error...)."""
    data: object = None
    text = stdout.strip()
    if text:
        try:
            data = json.loads(text.splitlines()[-1] if not text.startswith("{") else text)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        detail = (stderr or stdout).strip()[-1500:] or f"código {returncode}"
        return ClaudeResult(False, "", None, None, None, error=f"saída inesperada: {detail}")
    session = data.get("session_id")
    session_id = session if isinstance(session, str) and _SESSION_ID.match(session) else None
    reply = str(data.get("result") or "").strip()
    cost = data.get("total_cost_usd", data.get("cost_usd"))
    turns = data.get("num_turns")
    is_error = bool(data.get("is_error")) or data.get("subtype") not in (None, "success")
    error = None
    if is_error:
        error = reply or str(data.get("subtype") or "o Claude terminou com erro")
    return ClaudeResult(
        ok=not is_error and returncode == 0,
        reply=reply,
        session_id=session_id,
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        turns=int(turns) if isinstance(turns, int) else None,
        error=error if is_error else (None if returncode == 0 else f"código {returncode}"),
    )


class ClaudeCodeRunner:
    def __init__(self, settings: AssistantSettings, project_dir: Path) -> None:
        self._s = settings
        self._project = project_dir

    async def run(self, prompt: str) -> ClaudeResult:
        exe = find_claude(self._s)
        proc = await asyncio.create_subprocess_exec(
            *build_args(exe, self._s),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._project,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=self._s.timeout_s
            )
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        return parse_result(
            out.decode("utf-8", "replace"), proc.returncode, err.decode("utf-8", "replace")
        )

    def open_terminal(self, session_id: str) -> None:
        """Abre uma janela de terminal NESTE PC já dentro da conversa (claude --resume)."""
        if not _SESSION_ID.match(session_id):
            raise ValueError("session_id inválido")
        exe = find_claude(self._s)
        if sys.platform == "win32":
            # `start` abre uma janela nova; /k mantém o terminal aberto ao terminar.
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "Claude - cortesgta6", "cmd.exe", "/k", exe,
                 "--resume", session_id],
                cwd=self._project,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )  # fmt: skip
        else:
            raise RuntimeError(f"abrir terminal só no Windows; rode: claude --resume {session_id}")
