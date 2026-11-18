"""Entidades e objetos de valor do domínio.

Todos os timestamps "*_ts" são epoch em segundos (relógio de parede local, time.time()).
Offsets "*_s" são segundos relativos a algum início (arquivo, clipe).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Region:
    """Retângulo em frações do quadro de origem (0..1)."""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "w", "h"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"Region.{name} deve estar entre 0 e 1 (recebido {value})")
        if self.w <= 0 or self.h <= 0:
            raise ValueError("Region.w e Region.h devem ser > 0")
        if self.x + self.w > 1.0 + 1e-9 or self.y + self.h > 1.0 + 1e-9:
            raise ValueError("Region ultrapassa os limites do quadro")


class Platform(StrEnum):
    TWITCH = "twitch"
    KICK = "kick"
    YOUTUBE = "youtube"

    @property
    def label(self) -> str:
        return {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}[self.value]


def is_youtube_channel_id(login: str) -> bool:
    """IDs de canal do YouTube têm 24 caracteres e começam com "UC"."""
    return len(login) == 24 and login.startswith("UC")


def normalize_login(platform: Platform, login: str) -> str:
    login = login.strip().lstrip("@")
    if platform is Platform.YOUTUBE and is_youtube_channel_id(login):
        return login  # ID de canal diferencia maiúsculas
    return login.casefold()


_UNSAFE_PATH_CHARS = set('<>:"/\\|?*')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}  # fmt: skip


def safe_folder_name(name: str, fallback: str = "canal", max_len: int = 60) -> str:
    """Nome de pasta válido no Windows (e em qualquer sistema), legível para humanos."""
    cleaned = "".join(
        "_" if ch in _UNSAFE_PATH_CHARS or ord(ch) < 32 else ch for ch in name
    )
    cleaned = " ".join(cleaned.split())[:max_len].strip(" .")
    if not cleaned or set(cleaned) <= {"_"}:
        cleaned = fallback
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = f"{cleaned}_"
    return cleaned


def channel_key(platform: Platform, login: str) -> str:
    """Identificador único do canal entre plataformas (seguro para nome de arquivo)."""
    return f"{platform.value}-{normalize_login(platform, login)}"


@dataclass(frozen=True, slots=True)
class ChatRef:
    """Chat de outra plataforma lido junto com o canal principal (multistream)."""

    platform: Platform
    login: str
    chat_id: str | None = None  # Kick: id do chatroom, se a API pública estiver bloqueada

    def __post_init__(self) -> None:
        object.__setattr__(self, "login", normalize_login(self.platform, self.login))


@dataclass(frozen=True, slots=True)
class Channel:
    login: str
    platform: Platform = Platform.TWITCH
    display_name: str = ""
    language: str | None = None
    facecam: Region | None = None
    hashtags: tuple[str, ...] = ()
    chat_id: str | None = None  # Kick: id do chatroom (opcional; normalmente descoberto)
    extra_chats: tuple[ChatRef, ...] = ()
    quality: str | None = None  # qualidade de gravação só deste canal (None = recording.quality)
    folder: str | None = None  # pasta dos cortes (None = display_name ou login)

    def __post_init__(self) -> None:
        object.__setattr__(self, "login", normalize_login(self.platform, self.login))

    @property
    def key(self) -> str:
        return channel_key(self.platform, self.login)

    @property
    def name(self) -> str:
        return self.display_name or self.login

    @property
    def folder_name(self) -> str:
        """Pasta do criador em data/clips/pending/. Mesmo nome em duas plataformas = mesma
        pasta (os arquivos continuam com a plataforma no nome, então não colidem)."""
        return safe_folder_name(self.folder or self.name, fallback=self.key)

    @property
    def url(self) -> str:
        """URL curta usada nos créditos (na tela e na descrição)."""
        match self.platform:
            case Platform.TWITCH:
                return f"twitch.tv/{self.login}"
            case Platform.KICK:
                return f"kick.com/{self.login}"
            case Platform.YOUTUBE:
                if is_youtube_channel_id(self.login):
                    return f"youtube.com/channel/{self.login}"
                return f"youtube.com/@{self.login}"

    @property
    def watch_url(self) -> str:
        """URL que o streamlink abre para gravar a live.

        No YouTube é a página /live do canal (sempre aponta para a live atual), e não o
        watch?v=: se o streamer reiniciar a live, a gravação reconecta no vídeo novo.
        """
        if self.platform is Platform.YOUTUBE:
            return f"https://www.{self.url}/live"
        return f"https://{self.url}"


@dataclass(frozen=True, slots=True)
class LiveStream:
    key: str  # Channel.key
    user_name: str
    title: str
    game_name: str  # "" quando a plataforma não informa (YouTube)
    started_at: datetime | None = None
    viewer_count: int = 0
    # YouTube: id do vídeo da live; Kick: id do chatroom. Só agiliza a 1ª conexão do chat:
    # nas reconexões o adaptador descobre de novo (a live pode ter sido reiniciada).
    chat_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatMessage:
    channel: str
    author: str
    text: str
    ts: float


@dataclass(frozen=True, slots=True)
class ChatSignal:
    """Resultado da análise do chat em um instante."""

    ts: float
    rate: float  # mensagens/s na janela
    baseline: float  # média móvel de mensagens/s do canal
    z: float  # z-score da taxa contra a média móvel
    hype: float  # 0..1, densidade média de emotes/palavras de hype por mensagem
    diversity: float  # 0..1, autores únicos / mensagens
    score: float  # 0..100
    warm: bool  # baseline já confiável (passou do aquecimento)


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start: float
    end: float
    probability: float = 1.0  # confiança do Whisper (0..1); 1.0 = conferida/corrigida


@dataclass(frozen=True, slots=True)
class Transcript:
    language: str
    text: str
    words: tuple[Word, ...]
    # Sinais de qualidade (alimentam domain/caption_quality.py)
    language_probability: float | None = None
    language_forced: bool = False  # o idioma detectado não era permitido e foi trocado
    dropped_segments: int = 0  # trechos descartados como alucinação
    max_compression_ratio: float | None = None  # alto = texto repetitivo (loop)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Trecho do buffer copiado para a pasta do job (sobrevive à limpeza do buffer)."""

    path: Path
    wall_start: float
    wall_end: float


class MomentStatus(StrEnum):
    QUEUED = "queued"
    RENDERED = "rendered"
    REJECTED = "rejected"  # nota final abaixo do mínimo
    FAILED = "failed"


class ClipStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    DISCARDED = "discarded"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Moment:
    id: str
    channel: str  # Channel.key
    display_name: str
    peak_ts: float  # pico do chat
    event_ts: float  # momento estimado do lance (pico - atraso de reação do chat)
    start_ts: float  # início desejado do clipe
    end_ts: float  # fim desejado do clipe
    chat_score: float
    source_path: Path
    source_start_ts: float  # relógio de parede do primeiro quadro de source_path
    source_end_ts: float
    created_at: float
    status: MomentStatus = MomentStatus.QUEUED
    final_score: float | None = None
    error: str | None = None

    @property
    def clip_offset_s(self) -> float:
        """Offset do início do clipe dentro de source_path."""
        return max(0.0, self.start_ts - self.source_start_ts)

    @property
    def clip_duration_s(self) -> float:
        start = max(self.start_ts, self.source_start_ts)
        end = min(self.end_ts, self.source_end_ts)
        return max(0.0, end - start)

    @property
    def event_offset_in_clip_s(self) -> float:
        """Onde o lance cai dentro do clipe já cortado."""
        clip_start = max(self.start_ts, self.source_start_ts)
        return max(0.0, self.event_ts - clip_start)


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    moment_id: str
    attempts: int


@dataclass(frozen=True, slots=True)
class Clip:
    id: str
    moment_id: str
    channel: str  # Channel.key
    display_name: str
    video_path: Path
    description_path: Path
    score: float
    chat_score: float
    audio_score: float
    language: str
    transcript: str
    duration_s: float
    processing_s: float
    created_at: float
    status: ClipStatus = ClipStatus.PENDING_REVIEW
    caption_grade: str = "revisar"  # CaptionGrade: boa | revisar | ruim
    caption_score: float = 0.0  # confiança média da legenda, 0..100
    caption_notes: tuple[str, ...] = ()  # motivos da classificação
    doubtful_words: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Controls:
    """Pausas definidas pelo painel (persistem no banco entre reinícios)."""

    paused_all: bool = False  # para de gravar todos os canais
    worker_paused: bool = False  # para de gerar cortes (a fila espera)
    paused_channels: frozenset[str] = frozenset()  # Channel.key

    def channel_paused(self, key: str) -> bool:
        return self.paused_all or key in self.paused_channels


@dataclass(frozen=True, slots=True)
class RecorderStats:
    restarts: int  # quantas vezes streamlink/ffmpeg foram reiniciados nesta sessão
    last_segment_at: float | None  # relógio local em que o último segmento ficou pronto
    buffered_s: float  # segundos de vídeo no buffer agora


@dataclass(frozen=True, slots=True)
class SessionStats:
    started_at: float
    recorder: RecorderStats
    chat_rate: float  # msg/s na última avaliação
    chat_baseline: float
    chat_score: float
    chat_warm: bool
    last_message_at: float | None
    moments_detected: int
    moments_queued: int
    last_moment_at: float | None


@dataclass(frozen=True, slots=True)
class RenderRequest:
    source: Path
    offset_s: float
    duration_s: float
    captions: list[list[Word]]
    credit: str
    facecam: Region | None
    work_dir: Path
    output: Path
