"""Do momento capturado ao clipe final.

Ordem pensada para gastar GPU só com o que vale a pena:
  1. extrai o áudio (16 kHz mono) UMA vez: serve para a nota e para o Whisper;
  2. nota final = chat + áudio; abaixo do mínimo -> descarta antes de transcrever/renderizar;
  3. transcrição (slot de GPU limitado: o modelo fica na VRAM, 1 por vez por padrão);
  4. classifica a legenda (boa / revisar / ruim) pela confiança do Whisper;
  5. render 9:16 com legenda e crédito (slots NVENC limitados);
  6. descrição sugerida com crédito ao canal + relatório da legenda;
  7. guarda o kit de edição (trecho + legenda editável) para ajustes depois.

Organização: <output_dir>/<criador>/<pasta da qualidade>/<id>_notaNN.mp4 (+ .txt).
`rerender()` gera o corte de novo a partir do kit (legenda corrigida, trecho ajustado).
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from cortes.application.edit_kit import read_kit, write_kit
from cortes.domain.caption_quality import (
    CaptionGrade,
    CaptionQuality,
    CaptionQualityParams,
    assess_captions,
    quality_report,
    split_description,
)
from cortes.domain.captions import CaptionParams, chunk_words
from cortes.domain.models import (
    Channel,
    Clip,
    Moment,
    MomentStatus,
    Platform,
    RenderRequest,
    Transcript,
)
from cortes.domain.ports import MediaProcessor, MomentStore, Transcriber
from cortes.domain.publishing import build_description, build_hashtags, suggest_title
from cortes.domain.scoring import ScoreWeights, combine_scores

log = structlog.get_logger(__name__)

README_NAME = "_LEIA-ME.txt"

QUALITY_HELP: dict[CaptionGrade, str] = {
    CaptionGrade.GOOD: (
        "LEGENDA BOA\n\n"
        "Quase todas as palavras saíram com confiança alta no Whisper.\n"
        "Dá para postar sem revisar a legenda (assistir antes nunca é demais)."
    ),
    CaptionGrade.REVIEW: (
        "REVISAR A LEGENDA\n\n"
        "Uma ou outra palavra duvidosa, idioma corrigido, trecho descartado ou corte sem fala.\n"
        "Assista antes de postar. O motivo de cada corte está no fim do .txt dele e no painel.\n"
        "Para corrigir: peça ao Claude pelo painel, ou edite transcript.json no kit de edição\n"
        "e rode `uv run cortes rerender <id>`."
    ),
    CaptionGrade.BAD: (
        "LEGENDA RUIM\n\n"
        "Muitas palavras duvidosas ou texto repetido em loop (o Whisper 'alucinou').\n"
        "Provavelmente a legenda precisa ser refeita antes de postar, ou o corte descartado.\n"
        "Causas comuns: música alta, várias pessoas falando junto, fala em outro idioma."
    ),
}


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    weights: ScoreWeights
    captions: CaptionParams
    output_dir: Path  # <output_dir>/<criador>/<qualidade>/
    work_root: Path  # pasta de trabalho do job = work_root / moment.id (vira o kit de edição)
    hashtags: tuple[str, ...]
    description_template: str
    title_max_chars: int = 90
    keep_work_files: bool = False  # mantém também o trecho de momentos descartados
    keep_edit_kit: bool = True  # mantém o kit de edição dos cortes prontos
    min_duration_s: float = 10.0
    platform_hashtags: Mapping[Platform, tuple[str, ...]] = field(default_factory=dict)
    quality: CaptionQualityParams = field(default_factory=CaptionQualityParams)
    quality_folders: Mapping[CaptionGrade, str] = field(
        default_factory=lambda: {
            CaptionGrade.GOOD: "1-legenda-boa",
            CaptionGrade.REVIEW: "2-legenda-revisar",
            CaptionGrade.BAD: "3-legenda-ruim",
        }
    )


class ClipPipeline:
    def __init__(
        self,
        *,
        store: MomentStore,
        media: MediaProcessor,
        transcriber: Transcriber,
        channel_lookup: Callable[[str], Channel],
        config: PipelineConfig,
        gpu_slots: asyncio.Semaphore,
        encode_slots: asyncio.Semaphore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._media = media
        self._transcriber = transcriber
        self._channel = channel_lookup
        self._cfg = config
        self._gpu = gpu_slots
        self._encode = encode_slots
        self._clock = clock

    # ------------------------------------------------------------------ corte novo
    async def process(self, moment_id: str) -> Clip | None:
        started = time.perf_counter()
        moment = await asyncio.to_thread(self._store.get_moment, moment_id)
        if moment is None:
            raise LookupError(f"momento {moment_id} não existe")
        if moment.status in (MomentStatus.RENDERED, MomentStatus.REJECTED):
            return None  # job repetido: já processado
        if not moment.source_path.is_file():
            raise FileNotFoundError(f"vídeo do momento não encontrado: {moment.source_path}")

        channel = self._channel(moment.channel)
        bound = log.bind(moment=moment.id, channel=moment.channel)
        duration = moment.clip_duration_s
        if duration < self._cfg.min_duration_s:
            await self._reject(moment, 0.0, f"trecho curto demais ({duration:.1f}s)")
            return None

        offset = moment.clip_offset_s
        t0 = time.perf_counter()
        pcm = await self._media.extract_audio(moment.source_path, offset, duration)
        audio_score = self._media.audio_excitement(pcm, moment.event_offset_in_clip_s)
        final = combine_scores(moment.chat_score, audio_score, self._cfg.weights)
        bound.info(
            "moment_scored",
            chat=round(moment.chat_score, 1),
            audio=round(audio_score, 2),
            final=round(final, 1),
            audio_ms=round((time.perf_counter() - t0) * 1000),
        )
        if final < self._cfg.weights.min_final_score:
            await self._reject(moment, final, "nota final abaixo do mínimo")
            return None

        t1 = time.perf_counter()
        async with self._gpu:
            transcript = await asyncio.to_thread(
                self._transcriber.transcribe, pcm, self._media.sample_rate, channel.language
            )
        transcribe_s = time.perf_counter() - t1
        quality = assess_captions(transcript, self._cfg.quality)

        output = self._output_path(channel, quality.grade, f"{moment.id}_nota{round(final):02d}")
        t2 = time.perf_counter()
        await self._render(
            moment.source_path, offset, duration, transcript, channel, moment, output
        )
        render_s = time.perf_counter() - t2

        title = suggest_title(transcript.text, channel, self._cfg.title_max_chars)
        description = build_description(
            self._cfg.description_template,
            title=title,
            channel=channel,
            hashtags=build_hashtags(
                self._cfg.hashtags,
                channel,
                self._cfg.platform_hashtags.get(channel.platform, ()),
            ),
        )
        description_path = output.with_suffix(".txt")
        await asyncio.to_thread(
            description_path.write_text,
            self._with_report(description, quality),
            "utf-8",
        )

        processing_s = time.perf_counter() - started
        clip = _with_quality(
            Clip(
                id=moment.id,
                moment_id=moment.id,
                channel=channel.key,
                display_name=channel.name,
                video_path=output,
                description_path=description_path,
                score=final,
                chat_score=moment.chat_score,
                audio_score=audio_score,
                language=transcript.language,
                transcript=transcript.text,
                duration_s=duration,
                processing_s=processing_s,
                created_at=self._clock(),
            ),
            quality,
        )
        await asyncio.to_thread(self._store.add_clip, clip)
        await asyncio.to_thread(
            self._store.set_moment_status, moment.id, MomentStatus.RENDERED, final_score=final
        )
        # Kit de edição só quando o trecho está na pasta do job (no render-file o vídeo é
        # um arquivo seu, fora de data/work: nada é guardado nem apagado dele).
        if self._cfg.keep_edit_kit and moment.source_path.parent == self._work_dir(moment):
            await asyncio.to_thread(write_kit, self._work_dir(moment), transcript, offset, duration)
        else:
            await self._cleanup(moment)
        bound.info(
            "clip_rendered",
            path=str(output),
            score=round(final, 1),
            caption=quality.grade.value,
            caption_confidence=quality.score,
            language=transcript.language,
            words=len(transcript.words),
            transcribe_s=round(transcribe_s, 2),
            render_s=round(render_s, 2),
            total_s=round(processing_s, 2),
            realtime_factor=round(duration / processing_s, 2) if processing_s else None,
        )
        return clip

    # ------------------------------------------------------------------ refazer corte
    async def rerender(self, clip_id: str) -> Clip:
        """Gera o corte de novo a partir do kit de edição (legenda/tempos editados).

        Reclassifica a legenda e, se a classificação mudar, move o corte (e o .txt) para
        a pasta nova. O texto do post no .txt é mantido; só o relatório é atualizado.
        """
        clip = await asyncio.to_thread(self._store.get_clip, clip_id)
        if clip is None:
            raise LookupError(f"corte {clip_id} não existe")
        moment = await asyncio.to_thread(self._store.get_moment, clip.moment_id)
        if moment is None:
            raise LookupError(f"momento {clip.moment_id} não existe")
        kit = await asyncio.to_thread(read_kit, self._work_dir(moment))
        channel = self._channel(moment.channel)
        quality = assess_captions(kit.transcript, self._cfg.quality)
        output = self._output_path(channel, quality.grade, clip.video_path.stem)

        started = time.perf_counter()
        await self._render(
            kit.source, kit.offset_s, kit.duration_s, kit.transcript, channel, moment, output
        )

        old_text = ""
        if clip.description_path.is_file():
            old_text = await asyncio.to_thread(clip.description_path.read_text, "utf-8")
        post = split_description(old_text) if old_text.strip() else ""
        if not post:
            post = build_description(
                self._cfg.description_template,
                title=suggest_title(kit.transcript.text, channel, self._cfg.title_max_chars),
                channel=channel,
                hashtags=build_hashtags(
                    self._cfg.hashtags,
                    channel,
                    self._cfg.platform_hashtags.get(channel.platform, ()),
                ),
            )
        description_path = output.with_suffix(".txt")
        await asyncio.to_thread(
            description_path.write_text, self._with_report(post, quality), "utf-8"
        )
        for old, new in ((clip.video_path, output), (clip.description_path, description_path)):
            if old.resolve() != new.resolve():
                old.unlink(missing_ok=True)

        updated = _with_quality(
            dataclasses.replace(
                clip,
                video_path=output,
                description_path=description_path,
                transcript=kit.transcript.text,
                duration_s=kit.duration_s,
            ),
            quality,
        )
        await asyncio.to_thread(self._store.add_clip, updated)
        log.info(
            "clip_rerendered",
            clip=clip.id,
            caption=quality.grade.value,
            caption_confidence=quality.score,
            moved=clip.video_path.resolve() != output.resolve(),
            path=str(output),
            render_s=round(time.perf_counter() - started, 2),
        )
        return updated

    # ------------------------------------------------------------------ internos
    async def _render(
        self,
        source: Path,
        offset: float,
        duration: float,
        transcript: Transcript,
        channel: Channel,
        moment: Moment,
        output: Path,
    ) -> None:
        async with self._encode:
            await self._media.render_vertical(
                RenderRequest(
                    source=source,
                    offset_s=offset,
                    duration_s=duration,
                    captions=chunk_words(transcript.words, self._cfg.captions),
                    credit=channel.url,
                    facecam=channel.facecam,
                    work_dir=self._work_dir(moment),
                    output=output,
                )
            )

    def _output_path(self, channel: Channel, grade: CaptionGrade, stem: str) -> Path:
        folder = self._cfg.output_dir / channel.folder_name / self._cfg.quality_folders[grade]
        folder.mkdir(parents=True, exist_ok=True)
        readme = folder / README_NAME
        if not readme.exists():
            readme.write_text(QUALITY_HELP[grade] + "\n", encoding="utf-8")
        return folder / f"{stem}.mp4"

    def _with_report(self, post: str, quality: CaptionQuality) -> str:
        folder = self._cfg.quality_folders[quality.grade]
        return f"{post.rstrip()}\n\n{quality_report(quality, folder)}"

    async def _reject(self, moment: Moment, final: float, reason: str) -> None:
        log.info("moment_rejected", moment=moment.id, final=round(final, 1), reason=reason)
        await asyncio.to_thread(
            self._store.set_moment_status,
            moment.id,
            MomentStatus.REJECTED,
            final_score=final,
            error=reason,
        )
        if not self._cfg.keep_work_files:
            await self._cleanup(moment)

    def _work_dir(self, moment: Moment) -> Path:
        return self._cfg.work_root / moment.id

    async def _cleanup(self, moment: Moment) -> None:
        """Apaga só a pasta de trabalho do job (nunca o vídeo original do render-file)."""
        await asyncio.to_thread(shutil.rmtree, self._work_dir(moment), True)


def _with_quality(clip: Clip, quality: CaptionQuality) -> Clip:
    return dataclasses.replace(
        clip,
        caption_grade=quality.grade.value,
        caption_score=quality.score,
        caption_notes=quality.reasons,
        doubtful_words=tuple(f"{w} ({round(p * 100)}%)" for w, p in quality.doubtful_words),
    )
