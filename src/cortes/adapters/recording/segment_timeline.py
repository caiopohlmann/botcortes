"""Linha do tempo dos segmentos gravados: mapeia tempo do stream (PTS) -> relógio de parede.

Por que não usar a hora em que o arquivo apareceu? O HLS chega em rajadas (o streamlink
baixa vários segmentos de uma vez ao conectar), então a hora de chegada oscila. O PTS
é contínuo e exato; só falta o deslocamento até o relógio local. Usamos o MENOR atraso
observado (agora - fim_pts) numa janela recente: a observação com menor atraso é a que
chegou "na hora", e rajadas atrasadas não distorcem. A janela deslizante deixa o
deslocamento se readaptar após intervalos de anúncio ou travadas da rede.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Segment:
    run_id: int  # cada (re)início do FFmpeg é uma "run" com PTS próprio
    path: Path
    pts_start: float
    pts_end: float
    wall_start: float
    wall_end: float

    @property
    def duration(self) -> float:
        return self.pts_end - self.pts_start


class SegmentTimeline:
    def __init__(self, offset_window: int = 30) -> None:
        self._offsets: deque[float] = deque(maxlen=offset_window)
        self._segments: deque[Segment] = deque()
        self._run_id = 0
        self._last_pts_end: float | None = None

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(self._segments)

    @property
    def run_id(self) -> int:
        return self._run_id

    def new_run(self) -> int:
        """Chamado quando o FFmpeg reinicia: PTS recomeça e o deslocamento muda."""
        self._run_id += 1
        self._offsets.clear()
        self._last_pts_end = None
        return self._run_id

    def observe(self, path: Path, pts_start: float, pts_end: float, observed_at: float) -> Segment:
        if self._last_pts_end is not None and pts_end < self._last_pts_end - 1.0:
            # PTS voltou no tempo dentro da mesma run (descontinuidade): recomeça o ajuste.
            self._offsets.clear()
        self._last_pts_end = pts_end
        self._offsets.append(observed_at - pts_end)
        offset = min(self._offsets)
        segment = Segment(
            run_id=self._run_id,
            path=path,
            pts_start=pts_start,
            pts_end=pts_end,
            wall_start=pts_start + offset,
            wall_end=pts_end + offset,
        )
        self._segments.append(segment)
        return segment

    @property
    def buffered_s(self) -> float:
        return sum(s.duration for s in self._segments)

    def between(self, start_ts: float, end_ts: float) -> list[Segment]:
        """Segmentos que tocam [start_ts, end_ts], todos da mesma run.

        Concatenar runs diferentes geraria um arquivo com PTS descontínuo; nesse caso
        ficamos com a run que contém o fim do intervalo (o lance costuma estar no fim).
        """
        hits = [s for s in self._segments if s.wall_end > start_ts and s.wall_start < end_ts]
        if not hits:
            return []
        last_run = hits[-1].run_id
        return [s for s in hits if s.run_id == last_run]

    def expire(self, older_than_ts: float) -> list[Segment]:
        expired: list[Segment] = []
        while self._segments and self._segments[0].wall_end < older_than_ts:
            expired.append(self._segments.popleft())
        return expired
