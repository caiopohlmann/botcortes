"""Agrupa palavras transcritas em blocos curtos estilo shorts e calcula o destaque.

Formato-agnóstico: produz eventos (início, fim, palavras, índice destacado). O
adaptador de legenda (ASS) só formata o resultado.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from cortes.domain.models import Word

_SENTENCE_END = (".", "!", "?", "…")
_MIN_EVENT_S = 0.05


@dataclass(frozen=True, slots=True)
class CaptionParams:
    max_words: int = 3
    max_chars: int = 14
    max_gap_s: float = 0.6  # pausa maior que isso quebra o bloco
    linger_s: float = 0.3  # quanto o bloco fica na tela depois da última palavra
    uppercase: bool = True


@dataclass(frozen=True, slots=True)
class CaptionEvent:
    start: float
    end: float
    words: tuple[str, ...]
    highlight: int  # índice da palavra falada neste intervalo


def chunk_words(words: Iterable[Word], params: CaptionParams) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        text = " ".join(word.text.split())
        if not text:
            continue
        if params.uppercase:
            text = text.upper()
        word = Word(
            text=text,
            start=word.start,
            end=max(word.end, word.start),
            probability=word.probability,
        )
        if current:
            chars = sum(len(w.text) for w in current) + len(current) + len(text)
            gap = word.start - current[-1].end
            if (
                len(current) >= params.max_words
                or chars > params.max_chars
                or gap > params.max_gap_s
                or current[-1].text.endswith(_SENTENCE_END)
            ):
                chunks.append(current)
                current = []
        current.append(word)
    if current:
        chunks.append(current)
    return chunks


def caption_events(chunks: Sequence[Sequence[Word]], params: CaptionParams) -> list[CaptionEvent]:
    """Um evento por palavra: o bloco inteiro fica visível e a palavra atual é destacada.

    Os eventos de um bloco são contíguos (sem "piscar" entre palavras) e nunca invadem
    o bloco seguinte.
    """
    events: list[CaptionEvent] = []
    for ci, chunk in enumerate(chunks):
        if not chunk:
            continue
        texts = tuple(w.text for w in chunk)
        next_start = chunks[ci + 1][0].start if ci + 1 < len(chunks) and chunks[ci + 1] else None
        chunk_end = chunk[-1].end + params.linger_s
        if next_start is not None:
            chunk_end = min(chunk_end, next_start)
        chunk_end = max(chunk_end, chunk[-1].end)

        for wi, word in enumerate(chunk):
            start = chunk[0].start if wi == 0 else word.start
            end = chunk[wi + 1].start if wi + 1 < len(chunk) else chunk_end
            if events and start < events[-1].end:
                start = events[-1].end
            if end - start < _MIN_EVENT_S:
                end = start + _MIN_EVENT_S
            events.append(CaptionEvent(start=start, end=end, words=texts, highlight=wi))
    return events
