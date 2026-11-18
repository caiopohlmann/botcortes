from __future__ import annotations

from cortes.domain.captions import CaptionParams, caption_events, chunk_words
from cortes.domain.models import Word

P = CaptionParams(max_words=3, max_chars=14, max_gap_s=0.6, linger_s=0.3, uppercase=True)


def words(*items: tuple[str, float, float]) -> list[Word]:
    return [Word(t, s, e) for t, s, e in items]


def test_chunks_by_word_count() -> None:
    ws = words(("a", 0, 0.1), ("b", 0.1, 0.2), ("c", 0.2, 0.3), ("d", 0.3, 0.4))
    chunks = chunk_words(ws, P)
    assert [[w.text for w in c] for c in chunks] == [["A", "B", "C"], ["D"]]


def test_chunks_by_chars() -> None:
    ws = words(("socorro", 0, 0.3), ("mano", 0.3, 0.5), ("sério", 0.5, 0.8))
    chunks = chunk_words(ws, P)
    assert [[w.text for w in c] for c in chunks] == [["SOCORRO", "MANO"], ["SÉRIO"]]


def test_chunks_break_on_pause_and_punctuation() -> None:
    ws = words(("oi", 0, 0.2), ("gente", 1.5, 1.8), ("olha!", 1.8, 2.0), ("isso", 2.0, 2.2))
    chunks = chunk_words(ws, P)
    assert [[w.text for w in c] for c in chunks] == [["OI"], ["GENTE", "OLHA!"], ["ISSO"]]


def test_skips_empty_words() -> None:
    assert chunk_words(words((" ", 0, 1), ("", 1, 2)), P) == []


def test_events_one_per_word_contiguous() -> None:
    chunks = chunk_words(words(("um", 0.0, 0.3), ("dois", 0.35, 0.6), ("tres", 0.7, 1.0)), P)
    events = caption_events(chunks, P)
    assert [e.highlight for e in events] == [0, 1, 2]
    assert all(e.words == ("UM", "DOIS", "TRES") for e in events)
    for a, b in zip(events, events[1:], strict=False):
        assert a.end == b.start  # sem "piscar" entre palavras
    assert events[-1].end == 1.0 + P.linger_s


def test_events_never_overlap_next_chunk() -> None:
    ws = words(("a", 0, 0.1), ("b", 0.1, 0.2), ("c", 0.2, 0.3), ("d", 0.4, 0.6))
    events = caption_events(chunk_words(ws, P), P)
    for a, b in zip(events, events[1:], strict=False):
        assert a.end <= b.start + 1e-9
