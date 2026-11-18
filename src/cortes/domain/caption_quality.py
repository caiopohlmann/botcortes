"""Avalia a qualidade da legenda gerada pelo Whisper e separa os cortes em 3 grupos.

O Whisper devolve uma confiança (0..1) para cada palavra, e alguns sinais de que
"alucinou": texto repetido em loop (taxa de compressão alta), idioma trocado, trechos
descartados. Sem um gabarito não dá para saber se a legenda está certa; dá para saber
quando o próprio modelo estava inseguro. É isso que a nota mede.

  boa     -> quase todas as palavras com confiança alta: pode postar sem revisar
  revisar -> algumas palavras duvidosas, idioma corrigido, trecho descartado ou sem fala
  ruim    -> muitas palavras duvidosas ou repetição em loop: provavelmente refazer

Os limites são um ponto de partida (calibre com cortes reais em `caption_quality`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cortes.domain.models import Transcript, Word


class CaptionGrade(StrEnum):
    GOOD = "boa"
    REVIEW = "revisar"
    BAD = "ruim"


@dataclass(frozen=True, slots=True)
class CaptionQualityParams:
    low_word_probability: float = 0.5  # abaixo disso a palavra conta como "duvidosa"
    good_max_low_ratio: float = 0.10  # até 10% de palavras duvidosas = boa
    good_min_mean: float = 0.80
    bad_min_low_ratio: float = 0.30  # mais de 30% de palavras duvidosas = ruim
    bad_max_mean: float = 0.60
    max_compression_ratio: float = 2.4  # acima disso o Whisper costuma estar em loop
    repeat_run: int = 5  # mesma palavra 5x seguidas = loop
    min_language_probability: float = 0.6
    max_listed_doubts: int = 8


@dataclass(frozen=True, slots=True)
class CaptionQuality:
    grade: CaptionGrade
    mean_probability: float | None  # 0..1 (None = sem fala)
    low_ratio: float | None  # fração de palavras duvidosas
    reasons: tuple[str, ...]  # por que caiu nesse grupo (português, para humanos)
    doubtful_words: tuple[tuple[str, float], ...]  # (palavra, confiança), piores primeiro

    @property
    def score(self) -> float:
        """0..100 para ordenar/mostrar: a confiança média das palavras."""
        if self.mean_probability is None:
            return 0.0
        return round(self.mean_probability * 100, 1)


def _longest_repeat(words: tuple[Word, ...]) -> tuple[int, str]:
    best, best_word, run, previous = 0, "", 0, None
    for word in words:
        token = word.text.strip(".,!?;:…\"'").casefold()
        if not token:
            continue
        run = run + 1 if token == previous else 1
        previous = token
        if run > best:
            best, best_word = run, token
    return best, best_word


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def assess_captions(transcript: Transcript, params: CaptionQualityParams) -> CaptionQuality:
    words = transcript.words
    if not words:
        return CaptionQuality(
            grade=CaptionGrade.REVIEW,
            mean_probability=None,
            low_ratio=None,
            reasons=("sem fala detectada: o corte saiu sem legenda",),
            doubtful_words=(),
        )

    probabilities = [w.probability for w in words]
    mean = sum(probabilities) / len(probabilities)
    low = [w for w in words if w.probability < params.low_word_probability]
    low_ratio = len(low) / len(words)
    doubts = tuple(
        (w.text, round(w.probability, 2))
        for w in sorted(low, key=lambda w: w.probability)[: params.max_listed_doubts]
    )

    bad: list[str] = []
    review: list[str] = []

    if low_ratio > params.bad_min_low_ratio:
        bad.append(f"{_pct(low_ratio)} das palavras com confiança baixa")
    elif low_ratio > params.good_max_low_ratio:
        review.append(f"{_pct(low_ratio)} das palavras com confiança baixa")
    if mean < params.bad_max_mean:
        bad.append(f"confiança média baixa ({_pct(mean)})")
    elif mean < params.good_min_mean:
        review.append(f"confiança média de {_pct(mean)}")

    run, repeated = _longest_repeat(words)
    if run >= params.repeat_run:
        bad.append(f'"{repeated}" repetido {run}x seguidas (sinal de alucinação do Whisper)')
    ratio = transcript.max_compression_ratio
    if ratio is not None and ratio > params.max_compression_ratio:
        bad.append("texto muito repetitivo (sinal de alucinação do Whisper)")

    if transcript.language_forced:
        review.append(f"idioma detectado errado e corrigido para {transcript.language}")
    elif (
        transcript.language_probability is not None
        and transcript.language_probability < params.min_language_probability
    ):
        review.append(f"idioma incerto ({_pct(transcript.language_probability)} de certeza)")
    if transcript.dropped_segments:
        review.append(
            f"{transcript.dropped_segments} trecho(s) descartado(s) como alucinação "
            "(pode faltar fala na legenda)"
        )

    if bad:
        grade, reasons = CaptionGrade.BAD, (*bad, *review)
    elif review:
        grade, reasons = CaptionGrade.REVIEW, tuple(review)
    else:
        grade, reasons = CaptionGrade.GOOD, ()
    return CaptionQuality(
        grade=grade,
        mean_probability=mean,
        low_ratio=low_ratio,
        reasons=reasons,
        doubtful_words=doubts,
    )


def quality_report(quality: CaptionQuality, folder: str) -> str:
    """Bloco de texto que vai no fim do .txt do corte (separado do texto do post)."""
    lines = [REPORT_SEPARATOR, f"Legenda: {quality.grade.value.upper()} (pasta {folder})"]
    if quality.mean_probability is not None and quality.low_ratio is not None:
        lines.append(
            f"Confiança média {_pct(quality.mean_probability)} · "
            f"{_pct(quality.low_ratio)} das palavras duvidosas"
        )
    lines += [f"- {reason}" for reason in quality.reasons]
    if quality.doubtful_words:
        doubts = ", ".join(f'"{w}" ({_pct(p)})' for w, p in quality.doubtful_words)
        lines.append(f"Palavras duvidosas: {doubts}")
    return "\n".join(lines) + "\n"


REPORT_SEPARATOR = "---- qualidade da legenda (não copiar daqui para baixo) ----"


def split_description(text: str) -> str:
    """Só o texto do post (sem o relatório de qualidade)."""
    return text.split(REPORT_SEPARATOR, 1)[0].rstrip() + "\n"
