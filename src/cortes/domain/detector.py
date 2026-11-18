"""Transforma a série de sinais do chat em momentos discretos.

Máquina de estados simples:
  ocioso --(score >= trigger, z >= z_trigger, fora do cooldown)--> episódio
  episódio --(score < release ou duração máxima)--> emite o pico do episódio

O pico emitido é o sinal de maior score dentro do episódio. Isso funde automaticamente
as várias "ondas" de um mesmo lance num único clipe; o cooldown impede que um segundo
episódio logo depois gere um clipe quase idêntico.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cortes.domain.models import ChatSignal


@dataclass(frozen=True, slots=True)
class DetectorParams:
    trigger_score: float = 45.0
    release_score: float = 25.0
    z_trigger: float = 3.0
    max_episode_s: float = 30.0
    cooldown_s: float = 90.0

    def __post_init__(self) -> None:
        if self.release_score > self.trigger_score:
            raise ValueError("release_score deve ser <= trigger_score")


@dataclass(frozen=True, slots=True)
class DetectedPeak:
    peak_ts: float
    score: float
    signal: ChatSignal


class MomentDetector:
    def __init__(self, params: DetectorParams) -> None:
        self._p = params
        self._episode_start: float | None = None
        self._best: ChatSignal | None = None
        self._last_peak_ts = -math.inf

    @property
    def in_episode(self) -> bool:
        return self._best is not None

    def feed(self, signal: ChatSignal) -> DetectedPeak | None:
        p = self._p
        if self._best is None or self._episode_start is None:
            if (
                signal.warm
                and signal.ts - self._last_peak_ts >= p.cooldown_s
                and signal.score >= p.trigger_score
                and signal.z >= p.z_trigger
            ):
                self._episode_start = signal.ts
                self._best = signal
            return None

        if signal.score > self._best.score:
            self._best = signal
        if signal.score < p.release_score or signal.ts - self._episode_start >= p.max_episode_s:
            return self._close()
        return None

    def flush(self) -> DetectedPeak | None:
        """Fecha um episódio em andamento (ex.: a live acabou no meio do hype)."""
        return self._close() if self._best is not None else None

    def _close(self) -> DetectedPeak:
        best = self._best
        assert best is not None
        self._best = None
        self._episode_start = None
        self._last_peak_ts = best.ts
        return DetectedPeak(peak_ts=best.ts, score=best.score, signal=best)


@dataclass(frozen=True, slots=True)
class ClipParams:
    pre_roll_s: float = 40.0
    post_roll_s: float = 15.0
    # O chat reage alguns segundos depois do lance: o pico do chat fica "atrasado".
    reaction_offset_s: float = 4.0


@dataclass(frozen=True, slots=True)
class ClipWindow:
    event_ts: float
    start_ts: float
    end_ts: float

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts


def clip_window(peak_ts: float, params: ClipParams) -> ClipWindow:
    event = peak_ts - params.reaction_offset_s
    return ClipWindow(
        event_ts=event,
        start_ts=event - params.pre_roll_s,
        end_ts=event + params.post_roll_s,
    )
