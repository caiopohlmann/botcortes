"""Carrega configuração de config/settings.yaml (+ settings.<perfil>.yaml) e segredos do .env.

Os parâmetros de domínio (scoring, detector, clip, legendas) são dataclasses do próprio
domínio, validadas aqui pelo pydantic: o domínio continua sem depender de pydantic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from cortes.domain.caption_quality import CaptionQualityParams
from cortes.domain.captions import CaptionParams
from cortes.domain.clip_length import ShortClipParams
from cortes.domain.detector import ClipParams, DetectorParams
from cortes.domain.models import Channel, Platform, channel_key
from cortes.domain.scoring import ScoreWeights, ScoringParams


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    # Opcional: cookie "auth-token" de uma conta Turbo/inscrita (gravação sem anúncios)
    twitch_oauth_token: str = ""
    # Opcional: API oficial do Kick, usada só se a API pública for bloqueada (Cloudflare)
    kick_client_id: str = ""
    kick_client_secret: str = ""
    # Senha do painel. Obrigatória para abrir o painel na rede (celular etc.).
    panel_password: str = ""
    cortes_profile: str = "prod"
    cortes_config_dir: Path = Path("config")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PathsSettings(_Model):
    data_dir: Path = Path("data")
    # Abaixo disso de espaço livre no disco de data_dir, novos momentos não são capturados
    # (a gravação continua, mas nada vai para a fila) até liberar espaço.
    min_free_gb: float = 20.0

    @property
    def min_free_bytes(self) -> int:
        return int(self.min_free_gb * 1024**3)

    @property
    def buffers(self) -> Path:
        return self.data_dir / "buffers"

    @property
    def work(self) -> Path:
        return self.data_dir / "work"

    @property
    def clips(self) -> Path:
        """Cortes prontos: data/clips/<criador>/<N>.mp4 (+ .txt), numerados em sequência."""
        return self.data_dir / "clips"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"

    @property
    def database(self) -> Path:
        return self.data_dir / "cortes.db"


class LoggingSettings(_Model):
    level: str = "INFO"
    signal_log_interval_s: float = 0.0  # >0 registra o sinal do chat periodicamente (calibração)


class MonitorSettings(_Model):
    poll_interval_s: float = 30.0
    offline_grace_polls: int = 2  # polls "offline" seguidos antes de parar (evita falso offline)
    # Vazio = qualquer jogo. Só filtra quando a plataforma informa o jogo (YouTube não informa).
    game_filter: list[str] = Field(default_factory=list)


class TwitchSettings(_Model):
    disable_ads: bool = True  # pula os anúncios (o buffer fica sem vídeo durante eles)
    low_latency: bool = True
    streamlink_args: list[str] = Field(default_factory=list)  # ex.: token de conta Turbo/sub
    chat_delay_s: float = 0.0


class KickSettings(_Model):
    # Chat pelo mesmo WebSocket (Centrifugo) que a página do Kick usa. A URL do servidor
    # vem da negociação em `realtime_api`; `realtime_url` é a reserva, se ela falhar.
    realtime_api: str = "https://web.kick.com/api/v1/realtime"
    realtime_url: str = "wss://realtime.us-west-2.platform.kick.com/connection/websocket"
    streamlink_args: list[str] = Field(default_factory=list)
    chat_delay_s: float = 0.0


class YouTubeSettings(_Model):
    check_interval_s: float = 60.0  # de quanto em quanto tempo abre a página /live do canal
    chat_poll_min_s: float = 1.0
    chat_poll_max_s: float = 2.0
    # O chat do YouTube chega por polling (em lotes): a análise roda N s "atrasada" para
    # cada janela já estar completa. Os timestamps das mensagens são os do servidor.
    chat_delay_s: float = 3.0
    chat_max_age_s: float = 30.0  # mensagens mais velhas que isso ao chegar são histórico
    streamlink_args: list[str] = Field(default_factory=list)


class RecordingSettings(_Model):
    # None = usa o streamlink instalado no mesmo Python (python -m streamlink)
    streamlink_path: str | None = None
    ffmpeg_path: str = "ffmpeg"
    quality: str = "1080p60,1080p,936p60,720p60,best"
    segment_s: float = 2.0
    buffer_s: float = 180.0
    # Sem dados por N s -> reinicia. Precisa ser maior que um intervalo de anúncios da
    # Twitch (com disable_ads o streamlink fica mudo durante o anúncio, e reiniciar no
    # meio dele faz a Twitch mostrar um anúncio novo).
    stall_timeout_s: float = 120.0
    extra_streamlink_args: list[str] = Field(default_factory=list)  # todas as plataformas

    def streamlink_cmd(self) -> list[str]:
        if self.streamlink_path:
            return [self.streamlink_path]
        return [sys.executable, "-m", "streamlink"]


class AudioSettings(_Model):
    sample_rate: int = 16_000  # o Whisper exige 16 kHz; a análise de volume reaproveita
    before_event_s: float = 10.0
    after_event_s: float = 4.0
    range_db: float = 10.0  # subida de volume (dB) que já conta como 100%


class TranscriptionSettings(_Model):
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "float16"
    beam_size: int = 5
    allowed_languages: list[str] = Field(default_factory=lambda: ["pt", "en"])
    download_root: Path | None = None
    vad_filter: bool = True
    blocklist: list[str] = Field(default_factory=list)  # frases alucinadas a descartar


class CaptionStyle(_Model):
    font: str = "Arial Black"
    size: int = 80
    color: str = "#FFFFFF"
    highlight_color: str = "#FFE600"
    outline_color: str = "#000000"
    outline: float = 6.0
    shadow: float = 2.0
    highlight_scale: int = 112  # % de escala da palavra destacada
    credit_font: str = "Arial"
    credit_size: int = 40
    credit_margin_bottom: int = 110


class RenderSettings(_Model):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    hwaccel: str | None = "cuda"  # decodificação na GPU; None = CPU
    video_args: list[str] = Field(
        default_factory=lambda: [
            "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-cq", "21", "-b:v", "0", "-maxrate", "14M", "-bufsize", "28M",
            "-profile:v", "high", "-g", "60",
        ]
    )  # fmt: skip
    audio_bitrate: str = "160k"
    loudnorm: bool = True
    # Fração central da largura mantida (1.0 = 16:9 inteiro; 0.75 ≈ 4:3, gameplay maior)
    gameplay_width_fraction: float = Field(default=1.0, ge=0.5, le=1.0)
    gameplay_lift: int = 80  # sobe o gameplay (px) sem facecam, deixa espaço p/ legenda
    facecam_top: int = 70
    facecam_height: int = 560
    caption_gap: int = 60  # distância (px) entre o gameplay e a legenda
    fonts_dir: Path | None = None
    timeout_s: float = 600.0


class WorkerSettings(_Model):
    concurrency: int = 3  # jobs em paralelo (etapas de CPU se sobrepõem às de GPU)
    gpu_concurrency: int = 1  # transcrições simultâneas (VRAM)
    encode_concurrency: int = 2  # encodes NVENC simultâneos
    poll_s: float = 1.0
    job_timeout_s: float = 900.0
    max_attempts: int = 3
    keep_work_files: bool = False
    # Pastas em data/work mais velhas que isso (e cujo momento não está na fila) são
    # apagadas: sobras de jobs que falharam. 0 desliga a limpeza.
    work_retention_h: float = 24.0
    # Cada corte pronto guarda um "kit de edição" (trecho original + legenda editável) para
    # poder ser corrigido e gerado de novo (`cortes rerender`, ajuste pelo Claude no painel).
    # ~45 MB por corte; apagado depois de N dias.
    edit_kit_days: float = 7.0
    gpu_stats_interval_s: float = 60.0


class PanelSettings(_Model):
    enabled: bool = True  # `cortes run` sobe o painel junto
    # 127.0.0.1 = só neste PC. 0.0.0.0 = qualquer aparelho da rede (exige PANEL_PASSWORD).
    host: str = "127.0.0.1"
    port: int = 8080
    refresh_s: float = 5.0  # a página se atualiza sozinha a cada N s
    heartbeat_s: float = 5.0  # de quanto em quanto tempo cada processo publica o estado
    metrics_interval_s: float = 60.0  # amostra para os gráficos de 24 h
    metrics_retention_days: float = 7.0
    stale_after_s: float = 30.0  # sem batimento há N s = processo parado/travado


class AssistantSettings(_Model):
    """Atalho do painel "Pedir ao Claude": roda o Claude Code em segundo plano num corte."""

    enabled: bool = True
    claude_path: str = "claude"  # o Claude Code instalado e logado neste PC
    model: str | None = None  # None = o padrão da sua conta
    max_turns: int = 40
    timeout_s: float = 1200.0
    allow_ffmpeg: bool = True  # deixa o Claude usar ffmpeg/ffprobe para outras edições
    max_message_chars: int = 2000


class PublishSettings(_Model):
    hashtags: list[str] = Field(default_factory=lambda: ["#gta6", "#gtavi", "#shorts"])
    platform_hashtags: dict[Platform, list[str]] = Field(
        default_factory=lambda: {
            Platform.TWITCH: ["#twitch"],
            Platform.KICK: ["#kick"],
            Platform.YOUTUBE: [],
        }
    )
    title_max_chars: int = 90
    description_template: str = (
        "{title}\n\n"
        "🎮 Clipe da live de {display_name} - {channel_url}\n"
        "Créditos: {display_name} ({channel_url})\n\n"
        "{hashtags}\n"
    )


class Settings(_Model):
    profile: str = "prod"
    paths: PathsSettings = PathsSettings()
    logging: LoggingSettings = LoggingSettings()
    monitor: MonitorSettings = MonitorSettings()
    twitch: TwitchSettings = TwitchSettings()
    kick: KickSettings = KickSettings()
    youtube: YouTubeSettings = YouTubeSettings()
    recording: RecordingSettings = RecordingSettings()
    detection: ScoringParams = ScoringParams()
    detector: DetectorParams = DetectorParams()
    clip: ClipParams = ClipParams()
    short_clip: ShortClipParams = ShortClipParams()  # corte de 30 s (domain/clip_length.py)
    scoring: ScoreWeights = ScoreWeights()
    audio: AudioSettings = AudioSettings()
    transcription: TranscriptionSettings = TranscriptionSettings()
    captions: CaptionParams = CaptionParams()
    caption_quality: CaptionQualityParams = CaptionQualityParams()
    caption_style: CaptionStyle = CaptionStyle()
    render: RenderSettings = RenderSettings()
    worker: WorkerSettings = WorkerSettings()
    panel: PanelSettings = PanelSettings()
    assistant: AssistantSettings = AssistantSettings()
    publish: PublishSettings = PublishSettings()
    channels: list[Channel] = Field(default_factory=list)

    def channel(self, ref: str) -> Channel:
        """Busca por Channel.key ("kick-fulano") ou, se único, só pelo login ("fulano").

        Canal desconhecido vira um Channel padrão (Twitch, ou a plataforma do prefixo).
        """
        ref = ref.strip()
        folded = ref.casefold()
        for channel in self.channels:
            if channel.key.casefold() == folded:
                return channel
        by_login = [c for c in self.channels if c.login.casefold() == folded.lstrip("@")]
        if len(by_login) == 1:
            return by_login[0]
        for platform in Platform:
            prefix = f"{platform.value}-"
            if folded.startswith(prefix) and len(ref) > len(prefix):
                return Channel(login=ref[len(prefix) :], platform=platform)
        return Channel(login=ref)

    def chat_delay_s(self, platform: Platform) -> float:
        match platform:
            case Platform.TWITCH:
                return self.twitch.chat_delay_s
            case Platform.KICK:
                return self.kick.chat_delay_s
            case Platform.YOUTUBE:
                return self.youtube.chat_delay_s

    def streamlink_args(self, platform: Platform) -> list[str]:
        """Argumentos do streamlink específicos da plataforma (+ os comuns)."""
        args: list[str] = []
        match platform:
            case Platform.TWITCH:
                if self.twitch.disable_ads:
                    args.append("--twitch-disable-ads")
                if self.twitch.low_latency:
                    args.append("--twitch-low-latency")
                args += self.twitch.streamlink_args
            case Platform.KICK:
                args += self.kick.streamlink_args
            case Platform.YOUTUBE:
                args += self.youtube.streamlink_args
        return [*args, *self.recording.extra_streamlink_args]


def _check_unique_channels(channels: list[Channel]) -> None:
    keys: set[str] = set()
    for channel in channels:
        if channel.key in keys:
            raise ValueError(f"canal repetido em channels.yaml: {channel.key}")
        keys.add(channel.key)
    for channel in channels:
        for ref in channel.extra_chats:
            ref_key = channel_key(ref.platform, ref.login)
            if ref_key == channel.key:
                raise ValueError(f"{channel.key}: extra_chats repete o próprio canal")
            if ref_key in keys:
                # Seria gravado duas vezes e geraria clipes duplicados.
                raise ValueError(
                    f"{ref_key} está como canal e também em extra_chats de {channel.key}; "
                    "deixe só em um dos dois"
                )


def read_channels(path: Path) -> list[Channel]:
    """Só a lista de canais de um channels.yaml, com as mesmas validações do load_settings."""
    raw = _read_yaml(path).get("channels") or []
    channels = Settings.model_validate({"channels": raw}).channels
    _check_unique_channels(channels)
    return channels


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} deve conter um mapeamento YAML no topo")
    return data


def load_secrets() -> Secrets:
    return Secrets()


def load_settings(secrets: Secrets | None = None) -> Settings:
    secrets = secrets or load_secrets()
    config_dir = secrets.cortes_config_dir.resolve()
    profile = secrets.cortes_profile

    raw = _read_yaml(config_dir / "settings.yaml")
    raw = _deep_merge(raw, _read_yaml(config_dir / f"settings.{profile}.yaml"))
    raw["profile"] = profile
    raw["channels"] = _read_yaml(config_dir / "channels.yaml").get("channels") or []

    settings = Settings.model_validate(raw)
    _check_unique_channels(settings.channels)
    # O monitor, o worker e o painel releem só esta lista quando o arquivo muda
    # (cortes.channel_config.ChannelList); o resto do config vale até reiniciar.
    # Caminhos absolutos: os processos filhos e o FFmpeg (cwd diferente) enxergam o mesmo lugar.
    data_dir = settings.paths.data_dir
    if not data_dir.is_absolute():
        data_dir = (Path.cwd() / data_dir).resolve()
    paths = settings.paths.model_copy(update={"data_dir": data_dir})
    return settings.model_copy(update={"paths": paths})
