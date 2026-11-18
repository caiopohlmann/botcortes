"""Portas (interfaces) que a aplicação usa. Adaptadores implementam; o domínio não sabe
se por trás há Twitch, FFmpeg, SQLite, Redis etc.

Métodos de armazenamento são síncronos (SQLite é rápido); a camada de aplicação os
chama via asyncio.to_thread para não bloquear o event loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from cortes.domain.models import (
    Channel,
    ChatMessage,
    Clip,
    Job,
    LiveStream,
    Moment,
    MomentStatus,
    RecorderStats,
    RenderRequest,
    Snapshot,
    Transcript,
)


class StreamStatusProvider(Protocol):
    async def get_live_streams(
        self, channels: Sequence[Channel]
    ) -> Mapping[str, LiveStream | None]:
        """Status por Channel.key: LiveStream = ao vivo, None = offline confirmado.

        Canal ausente do resultado = status desconhecido nesta rodada (falha parcial):
        quem chama não deve iniciar nem parar a gravação dele por causa disso.
        """
        ...


class ChatSource(Protocol):
    def messages(self) -> AsyncIterator[ChatMessage]: ...


class SegmentRecorder(Protocol):
    async def run(self) -> None:
        """Grava até ser cancelado; reinicia sozinho se o processo cair."""
        ...

    async def snapshot(self, start_ts: float, end_ts: float, dest: Path) -> Snapshot | None: ...

    async def cleanup(self) -> None: ...

    def stats(self) -> RecorderStats: ...


class MomentStore(Protocol):
    def add_moment(self, moment: Moment) -> None: ...

    def get_moment(self, moment_id: str) -> Moment | None: ...

    def set_moment_status(
        self,
        moment_id: str,
        status: MomentStatus,
        *,
        final_score: float | None = None,
        error: str | None = None,
    ) -> None: ...

    def add_clip(self, clip: Clip) -> None:
        """Insere ou substitui (mesmo id) o registro do corte."""
        ...

    def get_clip(self, clip_id: str) -> Clip | None: ...


class JobQueue(Protocol):
    def enqueue(self, moment_id: str) -> int: ...

    def claim(self, lease_s: float) -> Job | None: ...

    def complete(self, job_id: int) -> None: ...

    def fail(self, job_id: int, error: str, max_attempts: int) -> None: ...

    def release(self, job_id: int) -> None: ...


class Transcriber(Protocol):
    def transcribe(self, pcm16: bytes, sample_rate: int, language: str | None) -> Transcript: ...


class MediaProcessor(Protocol):
    sample_rate: int

    async def extract_audio(self, source: Path, offset_s: float, duration_s: float) -> bytes:
        """PCM 16-bit mono em `sample_rate` Hz."""
        ...

    def audio_excitement(self, pcm16: bytes, event_s: float) -> float:
        """0..1: quanto o áudio perto do lance sobe em relação ao resto do trecho."""
        ...

    async def render_vertical(self, request: RenderRequest) -> None: ...
