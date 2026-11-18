"""Transcrição com faster-whisper (CTranslate2), palavra a palavra.

Performance:
- O modelo é carregado UMA vez por processo worker e fica residente na VRAM
  (large-v3-turbo float16 ≈ 3 GB numa RTX 3060 12 GB).
- O áudio chega já em memória (PCM 16 kHz extraído uma vez e reaproveitado da análise de
  volume): nada de arquivo temporário.
- VAD filter pula silêncio/música: menos tempo de GPU e menos alucinação.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from cortes.config import TranscriptionSettings
from cortes.domain.models import Transcript, Word

log = structlog.get_logger(__name__)

# Alucinações clássicas do Whisper em trechos sem fala (principalmente em PT).
_DEFAULT_BLOCKLIST = (
    "legendas pela comunidade",
    "amara.org",
    "obrigado por assistir",
    "inscreva-se no canal",
    "thanks for watching",
    "subtitles by",
)


def register_cuda_dlls() -> list[str]:
    """Windows: expõe as DLLs de cuBLAS/cuDNN instaladas via pip (pacotes nvidia-*).

    O CTranslate2 carrega cublas64_12.dll / cudnn*.dll pelo caminho de busca do sistema;
    sem isso seria preciso instalar o CUDA Toolkit e o cuDNN manualmente.
    """
    added: list[str] = []
    if sys.platform != "win32":
        return added
    import importlib.util

    for package in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime"):
        try:
            spec = importlib.util.find_spec(package)
        except ModuleNotFoundError:
            continue
        if spec is None or not spec.submodule_search_locations:
            continue
        for location in spec.submodule_search_locations:
            bin_dir = Path(location) / "bin"
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                added.append(str(bin_dir))
    return added


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class FasterWhisperTranscriber:
    def __init__(self, settings: TranscriptionSettings) -> None:
        self._s = settings
        if settings.device == "cuda":
            dirs = register_cuda_dlls()
            if dirs:
                log.info("cuda_dlls_registered", dirs=dirs)
        from faster_whisper import WhisperModel  # import tardio: só o worker paga o custo

        self._model: Any = WhisperModel(
            settings.model,
            device=settings.device,
            compute_type=settings.compute_type,
            download_root=str(settings.download_root) if settings.download_root else None,
        )
        self._allowed = tuple(settings.allowed_languages)
        self._blocklist = tuple(
            phrase.casefold() for phrase in (*_DEFAULT_BLOCKLIST, *settings.blocklist)
        )
        log.info(
            "whisper_loaded",
            model=settings.model,
            device=settings.device,
            compute_type=settings.compute_type,
        )

    def _run(self, audio: np.ndarray[Any, Any], language: str | None) -> tuple[Any, Any]:
        return self._model.transcribe(  # type: ignore[no-any-return]
            audio,
            language=language,
            word_timestamps=True,
            vad_filter=self._s.vad_filter,
            beam_size=self._s.beam_size,
            condition_on_previous_text=False,  # evita propagar alucinação entre trechos
        )

    def transcribe(self, pcm16: bytes, sample_rate: int, language: str | None) -> Transcript:
        if sample_rate != 16_000:
            raise ValueError("faster-whisper espera áudio a 16 kHz")
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0

        segments, info = self._run(audio, language)
        forced = False
        language_probability = _float_or_none(getattr(info, "language_probability", None))
        if language is None and self._allowed and info.language not in self._allowed:
            # Restringe a detecção automática aos idiomas permitidos (ex.: pt/en).
            probs = [p for p in (info.all_language_probs or []) if p[0] in self._allowed]
            chosen = max(probs, key=lambda p: p[1])[0] if probs else self._allowed[0]
            log.info("language_forced", detected=info.language, chosen=chosen)
            language_probability = float(max(probs, key=lambda p: p[1])[1]) if probs else None
            segments, info = self._run(audio, chosen)
            forced = True

        words: list[Word] = []
        texts: list[str] = []
        dropped = 0
        max_ratio: float | None = None
        for segment in segments:  # gerador preguiçoso: a inferência acontece aqui
            text = str(segment.text).strip()
            if self._is_hallucination(text, segment):
                dropped += 1
                continue
            ratio = _float_or_none(getattr(segment, "compression_ratio", None))
            if ratio is not None:
                max_ratio = ratio if max_ratio is None else max(max_ratio, ratio)
            texts.append(text)
            for w in segment.words or []:
                token = str(w.word).strip()
                if token:
                    probability = _float_or_none(getattr(w, "probability", None))
                    words.append(
                        Word(
                            text=token,
                            start=float(w.start),
                            end=float(w.end),
                            probability=1.0 if probability is None else probability,
                        )
                    )
        return Transcript(
            language=str(info.language),
            text=" ".join(texts),
            words=tuple(words),
            language_probability=language_probability,
            language_forced=forced,
            dropped_segments=dropped,
            max_compression_ratio=max_ratio,
        )

    def _is_hallucination(self, text: str, segment: Any) -> bool:
        lowered = text.casefold()
        if any(phrase in lowered for phrase in self._blocklist):
            return True
        no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
        avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
        return no_speech > 0.6 and avg_logprob < -1.0
