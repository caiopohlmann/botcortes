from __future__ import annotations

from cortes.domain.caption_quality import (
    REPORT_SEPARATOR,
    CaptionGrade,
    CaptionQualityParams,
    assess_captions,
    quality_report,
    split_description,
)
from cortes.domain.models import Transcript, Word

P = CaptionQualityParams()


def transcript(
    probs: list[float],
    *,
    language_forced: bool = False,
    dropped_segments: int = 0,
    max_compression_ratio: float | None = None,
) -> Transcript:
    words = tuple(Word(f"p{i}", i * 0.3, i * 0.3 + 0.25, p) for i, p in enumerate(probs))
    return Transcript(
        language="pt",
        text="",
        words=words,
        language_forced=language_forced,
        dropped_segments=dropped_segments,
        max_compression_ratio=max_compression_ratio,
    )


def test_confident_captions_are_good() -> None:
    quality = assess_captions(transcript([0.95] * 20), P)
    assert quality.grade is CaptionGrade.GOOD
    assert quality.reasons == () and quality.doubtful_words == ()
    assert quality.score == 95.0


def test_some_doubtful_words_need_review() -> None:
    quality = assess_captions(transcript([0.95] * 17 + [0.3, 0.4, 0.2]), P)  # 15% duvidosas
    assert quality.grade is CaptionGrade.REVIEW
    assert "15% das palavras com confiança baixa" in quality.reasons
    assert [w for w, _ in quality.doubtful_words] == ["p19", "p17", "p18"]  # piores primeiro


def test_many_doubtful_words_are_bad() -> None:
    quality = assess_captions(transcript([0.9] * 6 + [0.2] * 4), P)  # 40% duvidosas
    assert quality.grade is CaptionGrade.BAD


def test_repetition_loop_is_bad_even_with_confidence() -> None:
    words = tuple(Word("obrigado", i * 0.3, i * 0.3 + 0.2, 0.9) for i in range(6))
    quality = assess_captions(Transcript("pt", "", words), P)
    assert quality.grade is CaptionGrade.BAD
    assert any("repetido 6x" in r for r in quality.reasons)


def test_compression_ratio_flags_loop() -> None:
    quality = assess_captions(transcript([0.95] * 20, max_compression_ratio=3.1), P)
    assert quality.grade is CaptionGrade.BAD


def test_forced_language_and_dropped_segments_need_review() -> None:
    forced = assess_captions(transcript([0.95] * 20, language_forced=True), P)
    assert forced.grade is CaptionGrade.REVIEW
    dropped = assess_captions(transcript([0.95] * 20, dropped_segments=2), P)
    assert dropped.grade is CaptionGrade.REVIEW and "2 trecho(s)" in dropped.reasons[0]


def test_no_speech_goes_to_review() -> None:
    quality = assess_captions(Transcript("pt", "", ()), P)
    assert quality.grade is CaptionGrade.REVIEW
    assert quality.mean_probability is None and "sem fala" in quality.reasons[0]


def test_report_and_split() -> None:
    quality = assess_captions(transcript([0.95] * 17 + [0.3, 0.4, 0.2]), P)
    report = quality_report(quality)
    assert report.startswith(REPORT_SEPARATOR)
    assert "Legenda: REVISAR\n" in report and "pasta" not in report
    assert 'Palavras duvidosas: "p19" (20%)' in report
    text = "Título\n\n#gta6\n\n" + report
    assert split_description(text) == "Título\n\n#gta6\n"
