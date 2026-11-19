from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from cortes.application.clip_pipeline import ClipPipeline, PipelineConfig
from cortes.application.edit_kit import RENDER_FILE, TRANSCRIPT_FILE
from cortes.domain.caption_quality import REPORT_SEPARATOR
from cortes.domain.captions import CaptionParams
from cortes.domain.models import (
    Channel,
    Clip,
    Moment,
    MomentStatus,
    Platform,
    RenderRequest,
    Transcript,
    Word,
)
from cortes.domain.scoring import ScoreWeights

CHANNELS = {
    "twitch-fulano": Channel(login="fulano", display_name="Fulano"),
    "kick-fulano": Channel(login="fulano", platform=Platform.KICK, display_name="Fulano"),
    "kick-ciclano": Channel(login="ciclano", platform=Platform.KICK),
}

GOOD_WORDS = (Word("olha", 0.0, 0.3, 0.97), Word("isso.", 0.3, 0.6, 0.95))
BAD_WORDS = tuple(Word(w, i * 0.3, i * 0.3 + 0.2, 0.2) for i, w in enumerate(["a", "b", "c", "d"]))


class FakeStore:
    def __init__(self, moments: list[Moment]) -> None:
        self.moments = {m.id: m for m in moments}
        self.clips: dict[str, Clip] = {}
        self.status: dict[str, MomentStatus] = {}

    def add_moment(self, moment: Moment) -> None:
        self.moments[moment.id] = moment

    def get_moment(self, moment_id: str) -> Moment | None:
        return self.moments.get(moment_id)

    def set_moment_status(
        self,
        moment_id: str,
        status: MomentStatus,
        *,
        final_score: float | None = None,
        error: str | None = None,
    ) -> None:
        self.status[moment_id] = status

    def add_clip(self, clip: Clip) -> None:
        self.clips[clip.id] = clip

    def get_clip(self, clip_id: str) -> Clip | None:
        return self.clips.get(clip_id)

    def clip_video_paths(self) -> list[Path]:
        return [clip.video_path for clip in self.clips.values()]


class FakeMedia:
    sample_rate = 16_000

    def __init__(self, audio_score: float) -> None:
        self.audio_score = audio_score
        self.renders: list[RenderRequest] = []

    async def extract_audio(self, source: Path, offset_s: float, duration_s: float) -> bytes:
        return b"\x00\x00" * 16_000

    def audio_excitement(self, pcm16: bytes, event_s: float) -> float:
        return self.audio_score

    async def render_vertical(self, request: RenderRequest) -> None:
        self.renders.append(request)
        request.output.parent.mkdir(parents=True, exist_ok=True)
        request.output.write_bytes(b"mp4")


class FakeTranscriber:
    def __init__(self, words: tuple[Word, ...] = GOOD_WORDS) -> None:
        self.words = words

    def transcribe(self, pcm16: bytes, sample_rate: int, language: str | None) -> Transcript:
        return Transcript(language="pt", text="Olha isso.", words=self.words)


class Env:
    def __init__(self, words: tuple[Word, ...] = GOOD_WORDS, audio_score: float = 1.0) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cortes-pipeline-"))
        self.pending = self.root / "pending"
        self.work = self.root / "work"
        self.store = FakeStore([])
        self.media = FakeMedia(audio_score)
        self.pipeline = ClipPipeline(
            store=self.store,
            media=self.media,
            transcriber=FakeTranscriber(words),
            channel_lookup=lambda key: CHANNELS[key],
            config=PipelineConfig(
                weights=ScoreWeights(),
                captions=CaptionParams(),
                output_dir=self.pending,
                work_root=self.work,
                hashtags=("#gta6",),
                description_template="{title}\n{channel_url}\n{hashtags}",
                platform_hashtags={Platform.KICK: ("#kick",)},
            ),
            gpu_slots=asyncio.Semaphore(1),
            encode_slots=asyncio.Semaphore(1),
        )

    def add(
        self, moment_id: str, channel: str, chat_score: float = 90, hype_s: float | None = None
    ) -> Moment:
        """Momento com o trecho dentro de work/<id>/source.ts, como o monitor grava."""
        source = self.work / moment_id / "source.ts"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"ts")
        moment = Moment(
            id=moment_id,
            channel=channel,
            display_name=channel,
            peak_ts=1000.0,
            event_ts=1000.0,
            start_ts=957.0,  # 61 s em volta do lance (43 antes, 18 depois), como o monitor
            end_ts=1018.0,
            chat_score=chat_score,
            source_path=source,
            source_start_ts=950.0,
            source_end_ts=1020.0,
            created_at=1000.0,
            hype_s=hype_s,
        )
        self.store.add_moment(moment)
        return moment

    def run(self, *moment_ids: str) -> None:
        async def main() -> None:
            for moment_id in moment_ids:
                await self.pipeline.process(moment_id)

        asyncio.run(main())


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def test_one_folder_per_creator_with_numbered_clips() -> None:
    env = Env()
    env.add("20261118-210000-twitch-fulano-aaaa", "twitch-fulano")
    env.add("20261118-220000-kick-fulano-bbbb", "kick-fulano")
    env.add("20261118-230000-kick-ciclano-cccc", "kick-ciclano")
    env.run(
        "20261118-210000-twitch-fulano-aaaa",
        "20261118-220000-kick-fulano-bbbb",
        "20261118-230000-kick-ciclano-cccc",
    )
    assert names(env.pending) == ["Fulano", "ciclano"]
    # Mesmo nome em duas plataformas = mesma pasta e mesma sequência. Sem subpastas.
    assert names(env.pending / "Fulano") == ["1.mp4", "1.txt", "2.mp4", "2.txt"]
    assert names(env.pending / "ciclano") == ["1.mp4", "1.txt"]
    clip = env.store.clips["20261118-210000-twitch-fulano-aaaa"]
    assert clip.video_path == env.pending / "Fulano" / "1.mp4"
    assert clip.caption_grade == "boa" and clip.caption_score == 96.0


def test_numbering_never_repeats_after_deleting_published_clips() -> None:
    env = Env()
    env.add("m1", "twitch-fulano")
    env.add("m2", "twitch-fulano")
    env.run("m1", "m2")
    folder = env.pending / "Fulano"
    for name in ("1.mp4", "1.txt", "2.mp4", "2.txt"):  # publicados e apagados pelo dono
        (folder / name).unlink()
    (folder / "4 so YT.mp4").write_bytes(b"mp4")  # arquivo do dono, com número próprio
    (folder / "20261118-100000-twitch-fulano-zzzz_nota70.mp4").write_bytes(b"mp4")  # antigo
    env.add("m3", "twitch-fulano")
    env.run("m3")
    assert env.store.clips["m3"].video_path.name == "5.mp4"

    # Job refeito depois de gravar o corte: reaproveita o número, não cria outro.
    env.run("m3")
    assert sorted(p.name for p in folder.glob("*.mp4") if p.name[0] == "5") == ["5.mp4"]
    assert env.store.clips["m3"].video_path.name == "5.mp4"


def test_two_clips_finishing_together_get_different_numbers() -> None:
    env = Env()
    env.add("ma", "twitch-fulano")
    env.add("mb", "twitch-fulano")

    async def main() -> None:
        await asyncio.gather(env.pipeline.process("ma"), env.pipeline.process("mb"))

    asyncio.run(main())
    assert {env.store.clips[m].video_path.name for m in ("ma", "mb")} == {"1.mp4", "2.mp4"}


def test_quick_play_becomes_a_30s_clip_named_after_its_length() -> None:
    env = Env()
    env.add("m-rapido", "twitch-fulano", hype_s=5)  # chat explodiu e acalmou em 5 s
    env.add("m-longo", "twitch-fulano", hype_s=25)
    env.run("m-rapido", "m-longo")
    quick, long_one = env.store.clips["m-rapido"], env.store.clips["m-longo"]
    assert quick.video_path.name == "1 (30s).mp4" and quick.duration_s == 30.0
    assert long_one.video_path.name == "2.mp4" and long_one.duration_s == 61.0
    render = env.media.renders[0]
    assert render.offset_s == 7.0 + 43.0 - 22.0 and render.duration_s == 30.0
    text = quick.description_path.read_text(encoding="utf-8")
    assert "Duração: 30 s (lance rápido" in text.split(REPORT_SEPARATOR)[1]
    assert "Duração: 61 s (empolgação longa" in long_one.description_path.read_text("utf-8")


def test_bad_captions_keep_the_report_in_the_txt() -> None:
    env = Env(words=BAD_WORDS)
    env.add("m-bad", "kick-ciclano")
    env.run("m-bad")
    text = (env.pending / "ciclano" / "1.txt").read_text(encoding="utf-8")
    post, report = text.split(REPORT_SEPARATOR)
    assert "kick.com/ciclano" in post and "#gta6 #kick #ciclano" in post
    assert "Legenda: RUIM" in report and "pasta" not in report
    assert "100% das palavras com confiança baixa" in report
    clip = env.store.clips["m-bad"]
    assert clip.caption_grade == "ruim" and clip.doubtful_words


def test_edit_kit_is_kept_and_rerender_keeps_the_same_file() -> None:
    env = Env(words=BAD_WORDS)
    env.add("m-fix", "twitch-fulano")
    env.run("m-fix")
    kit = env.work / "m-fix"
    assert (kit / "source.ts").is_file()
    data = json.loads((kit / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert [w["text"] for w in data["words"]] == ["a", "b", "c", "d"] and "_ajuda" in data

    # O dono (ou o Claude) corrige a legenda e o texto do post, e corta 1 s do começo.
    data["words"] = [
        {"text": "olha", "start": 0.0, "end": 0.3, "probability": 1.0},
        {"text": "isso", "start": 0.3, "end": 0.6, "probability": 1.0},
    ]
    (kit / TRANSCRIPT_FILE).write_text(json.dumps(data), encoding="utf-8")
    render = json.loads((kit / RENDER_FILE).read_text(encoding="utf-8"))
    render["offset_s"] += 1
    render["duration_s"] -= 1
    (kit / RENDER_FILE).write_text(json.dumps(render), encoding="utf-8")
    old = env.store.clips["m-fix"]
    report = old.description_path.read_text(encoding="utf-8").split(REPORT_SEPARATOR)[1]
    old.description_path.write_text(
        "Meu título editado\n\n" + REPORT_SEPARATOR + report, encoding="utf-8"
    )

    updated = asyncio.run(env.pipeline.rerender("m-fix"))
    assert updated.caption_grade == "boa"
    assert updated.video_path == old.video_path  # mesmo número, mesma pasta
    assert updated.video_path.is_file() and updated.description_path == old.description_path
    text = updated.description_path.read_text(encoding="utf-8")
    assert text.startswith("Meu título editado")  # texto do post preservado
    assert "Legenda: BOA" in text
    request = env.media.renders[-1]
    assert request.offset_s == kit_offset(env) and request.duration_s == updated.duration_s
    assert [w.text for chunk in request.captions for w in chunk] == ["OLHA", "ISSO"]


def kit_offset(env: Env) -> float:
    return float(json.loads((env.work / "m-fix" / RENDER_FILE).read_text())["offset_s"])


def test_low_score_is_rejected_without_files() -> None:
    env = Env(audio_score=0.0)
    env.add("m-low", "twitch-fulano", chat_score=10)
    env.run("m-low")
    assert not env.pending.exists() or not any(env.pending.rglob("*.mp4"))
    assert env.store.status["m-low"] is MomentStatus.REJECTED
    assert env.store.clips == {}
    assert not (env.work / "m-low").exists()  # trecho descartado é apagado


def test_render_file_source_outside_work_is_never_touched() -> None:
    env = Env()
    own_video = Path(tempfile.mkdtemp(prefix="meu-video-")) / "vod.mp4"
    own_video.write_bytes(b"vod")
    env.store.add_moment(
        Moment(
            id="m-local",
            channel="twitch-fulano",
            display_name="Fulano",
            peak_ts=0,
            event_ts=40,
            start_ts=0,
            end_ts=55,
            chat_score=90,
            source_path=own_video,
            source_start_ts=0,
            source_end_ts=55,
            created_at=0,
        )
    )
    env.run("m-local")
    assert own_video.is_file()
    assert not (env.work / "m-local" / TRANSCRIPT_FILE).exists()
