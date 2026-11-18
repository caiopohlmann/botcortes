"""Atalho "Pedir ao Claude": monta o pedido e executa a fila (um pedido por vez).

O pedido leva tudo que o Claude precisa saber sobre o corte (arquivos, kit de edição,
classificação da legenda) e as regras do que ele pode fazer. Quem executa é uma porta
(`AssistantRunner`); o adaptador real é o Claude Code (adapters/assistant/claude_code.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ClipContext:
    clip_id: str
    creator: str
    channel_url: str
    video_path: Path
    description_path: Path
    kit_dir: Path
    kit_available: bool
    caption_grade: str
    caption_notes: tuple[str, ...]
    transcript: str


class RunnerResult(Protocol):
    @property
    def ok(self) -> bool: ...
    @property
    def reply(self) -> str: ...
    @property
    def session_id(self) -> str | None: ...
    @property
    def error(self) -> str | None: ...


class QueuedRequest(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def clip_id(self) -> str: ...
    @property
    def message(self) -> str: ...


class AssistantRunner(Protocol):
    async def run(self, prompt: str) -> RunnerResult: ...


def build_prompt(ctx: ClipContext, message: str) -> str:
    notes = "; ".join(ctx.caption_notes) or "nenhum problema apontado"
    kit = (
        f"""Kit de edição: {ctx.kit_dir}
  - transcript.json: legenda palavra por palavra, com tempos e confiança (EDITÁVEL)
  - render.json: onde o corte começa e quanto dura dentro do source.ts (EDITÁVEL)
  - source.ts: trecho original da live (não editar)"""
        if ctx.kit_available
        else f"Kit de edição: indisponível (apagado ou corte de render-file) em {ctx.kit_dir}. "
        "Sem ele não dá para refazer a legenda; só editar o texto do post ou usar ffmpeg."
    )
    return f"""Você está no projeto cortesgta6, um bot de cortes de lives (contexto: CLAUDE.md).
O dono pediu, pelo painel, um ajuste em UM corte. Faça só o que foi pedido.

Corte: {ctx.clip_id}
Criador: {ctx.creator} ({ctx.channel_url})
Vídeo atual: {ctx.video_path}
Texto do post + relatório da legenda: {ctx.description_path}
{kit}
Legenda classificada como: {ctx.caption_grade} ({notes})
Transcrição atual: "{ctx.transcript}"

Pedido do dono:
<<<
{message}
>>>

Como fazer:
1. Legenda (palavra errada, tempo, palavra a mais/faltando) ou corte do trecho (começo/fim):
   edite transcript.json e/ou render.json (as instruções estão no campo "_ajuda" de cada
   arquivo; palavra corrigida = "probability": 1.0) e rode:
   uv run cortes rerender {ctx.clip_id}
   O comando gera o vídeo de novo, reclassifica a legenda e move o corte de pasta se mudar.
2. Título, descrição ou hashtags: edite o .txt, só a parte ACIMA da linha
   "qualidade da legenda" (a parte de baixo é gerada pelo rerender).
3. Outras edições de vídeo (zoom, cortar, acelerar...): use ffmpeg gerando um arquivo NOVO
   ao lado do vídeo atual, com sufixo _editado.mp4. Não sobrescreva nem apague o original.
4. Não mexa em código, em config/, no .env, no banco ou em outros cortes. Não apague arquivos.
5. Se o pedido for ambíguo ou impossível com isso, não invente: explique o que faltou.

No fim, responda em português, em 2 a 5 linhas: o que mudou e onde está o resultado.
"""


async def assistant_loop(
    *,
    claim: Callable[[], QueuedRequest | None],
    finish: Callable[..., None],
    context_for: Callable[[str], ClipContext | None],
    runner: AssistantRunner,
    poll_s: float = 3.0,
) -> None:
    """Executa os pedidos da fila, um por vez. `claim/finish` vêm do banco do painel."""
    while True:
        request = await asyncio.to_thread(claim)
        if request is None:
            await asyncio.sleep(poll_s)
            continue
        request_id, clip_id, message = request.id, request.clip_id, request.message
        bound = log.bind(request=request_id, clip=clip_id)
        bound.info("assistant_started")
        try:
            ctx = await asyncio.to_thread(context_for, clip_id)
            if ctx is None:
                raise LookupError(f"corte {clip_id} não existe mais")
            result = await runner.run(build_prompt(ctx, message))
        except asyncio.CancelledError:
            await asyncio.to_thread(
                finish, request_id, ok=False, reply=None, session_id=None,
                error="interrompido: o painel foi encerrado. Envie de novo.",
            )  # fmt: skip
            raise
        except Exception as exc:
            bound.warning("assistant_failed", error=repr(exc))
            await asyncio.to_thread(
                finish, request_id, ok=False, reply=None, session_id=None, error=str(exc)
            )
            continue
        await asyncio.to_thread(
            finish,
            request_id,
            ok=result.ok,
            reply=result.reply,
            session_id=result.session_id,
            error=result.error,
        )
        bound.info("assistant_done", ok=result.ok, session=result.session_id)
