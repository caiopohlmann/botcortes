"""Do momento capturado ao clipe final.

Ordem pensada para gastar GPU só com o que vale a pena:
  1. extrai o áudio (16 kHz mono) UMA vez: serve para a nota e para o Whisper;
  2. nota final = chat + áudio; abaixo do mínimo -> descarta antes de transcrever/renderizar;
  3. transcrição (slot de GPU limitado: o modelo fica na VRAM, 1 por vez por padrão);
  4. classifica a legenda (boa / revisar / ruim) pela confiança do Whisper;
  5. render 9:16 com legenda e crédito (slots NVENC limitados);
  6. descrição sugerida com crédito ao canal + relatório da legenda;
  7. guarda o kit de edição (trecho + legenda editável) para ajustes depois.

Organização: <output_dir>/<criador>/<N>.mp4 (+ <N>.txt), numerados em sequência por criador
(1, 2, 3...). O número nunca se repete, mesmo depois de você apagar os já publicados: vale o
maior entre os arquivos da pasta e os cortes já registrados no banco. A qualidade da legenda
vai no fim do .txt e no painel.
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
    CaptionQuality,
    CaptionQualityParams,
    assess_captions,
    quality_report,
    split_description,
)
from cortes.domain.captions import CaptionParams, chunk_words
from cortes.domain.clip_length import ShortClipParams, choose_cut, cut_transcript, length_label
from cortes.domain.models import (
    Channel,
    Clip,
    Moment,
    MomentStatus,
    Platform,
    RenderRequest,
    Transcript,
    next_clip_number,
)
from cortes.domain.ports import MediaProcessor, MomentStore, Transcriber
from cortes.domain.publishing import build_description, build_hashtags, suggest_title
from cortes.domain.scoring import ScoreWeights, combine_scores

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    weights: ScoreWeights
    captions: CaptionParams
    output_dir: Path  # <output_dir>/<criador>/<N>.mp4
    work_root: Path  # pasta de trabalho do job = work_root / moment.id (vira o kit de edição)
    hashtags: tuple[str, ...]
    description_template: str
    title_max_chars: int = 90
    keep_work_files: bool = False  # mantém também o trecho de momentos descartados
    keep_edit_kit: bool = True  # mantém o kit de edição dos cortes prontos
    min_duration_s: float = 10.0
    platform_hashtags: Mapping[Platform, tuple[str, ...]] = field(default_factory=dict)
    quality: CaptionQualityParams = field(default_factory=CaptionQualityParams)
    short_clip: ShortClipParams = field(default_factory=ShortClipParams)


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
        # Números reservados por jobs em andamento (dois slots podem terminar juntos).
        self._numbering = asyncio.Lock()
        self._reserved: dict[Path, set[int]] = {}

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

        # 30 s (lance rápido, sem cortar fala) ou o trecho todo (1 min): ver domain/clip_length.
        cut = choose_cut(
            transcript.words,
            moment.event_offset_in_clip_s,
            duration,
            moment.hype_s,
            self._cfg.short_clip,
        )
        transcript = cut_transcript(transcript, cut)
        offset, duration = offset + cut.offset_s, cut.duration_s
        bound.info(
            "clip_length",
            short=cut.short,
            duration_s=round(duration, 1),
            hype_s=moment.hype_s,
            reason=cut.reason,
        )
        quality = assess_captions(transcript, self._cfg.quality)
        length_note = f"Duração: {duration:.0f} s ({cut.reason})"

        # Job refeito (falhou depois de gravar o corte): reaproveita o mesmo número.
        previous = await asyncio.to_thread(self._store.get_clip, moment.id)
        output = await self._output_path(
            channel, previous.video_path if previous else None, length_label(duration)
        )
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
            self._with_report(description, quality, length_note),
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
        output = await self._output_path(channel, clip.video_path)

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
            description_path.write_text,
            self._with_report(post, quality, f"Duração: {kit.duration_s:.0f} s"),
            "utf-8",
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

    async def _output_path(self, channel: Channel, current: Path | None, label: str = "") -> Path:
        """<criador>/<N>.mp4 ou "<N> (30s).mp4". Corte já na pasta do criador mantém o nome."""
        folder = self._cfg.output_dir / channel.folder_name
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        if current is not None and current.parent.resolve() == folder.resolve():
            return current
        async with self._numbering:
            used = await asyncio.to_thread(self._used_names, folder)
            reserved = self._reserved.setdefault(folder, set())
            number = next_clip_number([*used, *(str(n) for n in reserved)])
            reserved.add(number)
        return folder / (f"{number} {label}.mp4" if label else f"{number}.mp4")

    def _used_names(self, folder: Path) -> list[str]:
        """Nomes já usados na pasta: os arquivos de agora e os cortes do banco (apagados)."""
        names = [entry.name for entry in folder.iterdir()]
        target = folder.resolve()
        for path in self._store.clip_video_paths():
            if path.parent.resolve() == target:
                names.append(path.name)
        return names

    def _with_report(self, post: str, quality: CaptionQuality, note: str = "") -> str:
        """Texto do post + relatório da legenda (+ a duração escolhida e o motivo)."""
        report = quality_report(quality)
        if note:
            report += f"{note}\n"
        return f"{post.rstrip()}\n\n{report}"

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
