"""Escolhe a duração do corte: 30 s (lance rápido) ou 1 min (o trecho todo).

O monitor captura sempre o trecho longo (~61 s em volta do lance). Depois da transcrição,
o worker decide:
  - 30 s quando a empolgação do chat foi curta (lance rápido) E dá para cortar 30 s em
    volta do lance sem partir uma frase no meio (o início e o fim caem em silêncio);
  - senão, o trecho todo (1 min): empolgação longa, fala contínua ou momento antigo sem a
    duração da empolgação.
Corte curto só monetiza no YouTube Shorts; o TikTok pede 1 min ou mais.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from cortes.domain.models import Transcript, Word


@dataclass(frozen=True, slots=True)
class ShortClipParams:
    enabled: bool = True
    duration_s: float = 30.0
    before_event_s: float = 22.0  # o lance fica a ~3/4 do corte curto (contexto + reação)
    max_hype_s: float = 12.0  # empolgação do chat até isso = lance rápido
    search_s: float = 4.0  # quanto o início/fim podem andar para cair num silêncio
    silence_s: float = 0.35  # folga sem fala em volta do ponto de corte
    min_window_s: float = 45.0  # trecho capturado menor que isso: não escolhe (usa o que tem)


@dataclass(frozen=True, slots=True)
class ClipCut:
    offset_s: float  # início dentro do trecho capturado
    duration_s: float
    short: bool
    reason: str


def _quiet_at(words: Sequence[Word], t: float, margin: float) -> bool:
    """Nenhuma palavra sendo dita em [t - margin, t + margin]."""
    return all(w.end <= t - margin or w.start >= t + margin for w in words)


def _nearest_quiet(
    words: Sequence[Word], target: float, search: float, margin: float, lo: float, hi: float
) -> float | None:
    """Ponto de silêncio mais perto de `target` (passos de 0,1 s), dentro de [lo, hi]."""
    steps = int(search * 10)
    for i in range(steps + 1):
        for sign in (1, -1) if i else (1,):
            t = round(target + sign * i / 10, 2)
            if lo <= t <= hi and _quiet_at(words, t, margin):
                return t
    return None


def choose_cut(
    words: Sequence[Word],
    event_s: float,
    window_s: float,
    hype_s: float | None,
    params: ShortClipParams,
) -> ClipCut:
    """`words` e `event_s` em segundos desde o início do trecho capturado (de `window_s`)."""
    # short = o bot escolheu o corte de 30 s (o nome do arquivo vem da duração: length_label).
    whole = ClipCut(0.0, window_s, short=False, reason="")
    if not params.enabled:
        return replace(whole, reason="corte de 30 s desligado")
    if window_s < params.min_window_s:
        return replace(whole, reason=f"só havia {window_s:.0f} s gravados em volta do lance")
    if hype_s is None:
        return replace(whole, reason="sem a duração da empolgação do chat")
    if hype_s > params.max_hype_s:
        return replace(whole, reason=f"empolgação longa no chat ({hype_s:.0f} s)")

    length = params.duration_s
    target = min(max(0.0, event_s - params.before_event_s), window_s - length)
    margin, search = params.silence_s, params.search_s
    start = _nearest_quiet(words, target, search, margin, 0.0, window_s - length)
    if start is None:
        return replace(whole, reason="fala contínua no início do corte de 30 s")
    end = _nearest_quiet(words, start + length, search, margin, start + length - 2, window_s)
    if end is None:
        return replace(whole, reason="fala contínua no fim do corte de 30 s")
    return ClipCut(
        round(start, 2),
        round(end - start, 2),
        short=True,
        reason=f"lance rápido (empolgação de {hype_s:.0f} s) e cabe sem cortar fala",
    )


def cut_transcript(transcript: Transcript, cut: ClipCut) -> Transcript:
    """A transcrição do trecho escolhido, com os tempos a partir do novo início."""
    if cut.offset_s == 0 and not cut.short:
        return transcript
    end = cut.offset_s + cut.duration_s
    words = tuple(
        replace(w, start=round(w.start - cut.offset_s, 3), end=round(w.end - cut.offset_s, 3))
        for w in transcript.words
        if w.start >= cut.offset_s and w.end <= end
    )
    text = " ".join(w.text.strip() for w in words).strip()
    return replace(transcript, text=text, words=words)


def length_label(duration_s: float) -> str:
    """Sufixo do arquivo para corte com menos de 1 min ("(30s)"); vazio para 1 min ou mais."""
    return "" if duration_s >= 59.5 else f"({round(duration_s)}s)"
