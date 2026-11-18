from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from cortes.application.clip_pipeline import README_NAME, ClipPipeline, PipelineConfig
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
BAD_WORDS = tuple(Word(w, i * 0.3, i * 0.3 + 0.2, 0.2) for i, w in enumerate("a b c d".split()))


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

    def add(self, moment_id: str, channel: str, chat_score: float = 90) -> Moment:
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
            start_ts=960.0,
            end_ts=1015.0,
            chat_score=chat_score,
            source_path=source,
            source_start_ts=950.0,
            source_end_ts=1020.0,
            created_at=1000.0,
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


def test_one_folder_per_creator_then_per_caption_quality() -> None:
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
    assert names(env.pending / "Fulano") == ["1-legenda-boa"]
    good = env.pending / "Fulano" / "1-legenda-boa"
    assert names(good) == [
        "20261118-210000-twitch-fulano-aaaa_nota93.mp4",
        "20261118-210000-twitch-fulano-aaaa_nota93.txt",
        "20261118-220000-kick-fulano-bbbb_nota93.mp4",
        "20261118-220000-kick-fulano-bbbb_nota93.txt",
        README_NAME,
    ]
    assert "LEGENDA BOA" in (good / README_NAME).read_text(encoding="utf-8")
    clip = env.store.clips["20261118-210000-twitch-fulano-aaaa"]
    assert clip.caption_grade == "boa" and clip.caption_score == 96.0


def test_bad_captions_go_to_third_folder_with_report() -> None:
    env = Env(words=BAD_WORDS)
    env.add("m-bad", "kick-ciclano")
    env.run("m-bad")
    folder = env.pending / "ciclano" / "3-legenda-ruim"
    text = next(folder.glob("m-bad*.txt")).read_text(encoding="utf-8")
    post, report = text.split(REPORT_SEPARATOR)
    assert "kick.com/ciclano" in post and "#gta6 #kick #ciclano" in post
    assert "Legenda: RUIM (pasta 3-legenda-ruim)" in report
    assert "100% das palavras com confiança baixa" in report
    clip = env.store.clips["m-bad"]
    assert clip.caption_grade == "ruim" and clip.doubtful_words


def test_edit_kit_is_kept_and_rerender_fixes_and_moves() -> None:
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
    assert updated.video_path.parent.name == "1-legenda-boa"
    assert updated.video_path.name == old.video_path.name  # mesmo nome, outra pasta
    assert not old.video_path.exists() and not old.description_path.exists()
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
