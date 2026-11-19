"""Buffer circular de gravação: streamlink -> (relay) -> ffmpeg -c copy -f segment.

Performance:
- Sem reencode: o FFmpeg só remultiplexa em segmentos MPEG-TS de ~2 s (CPU quase zero).
- O relay em Python copia ~1 MB/s por canal, custo desprezível, e permite detectar
  travamento (sem dados por N s -> reinicia os processos).
- Segmentos antigos são apagados assim que saem da janela do buffer.
- Snapshot = concatenação binária dos segmentos .ts (TS concatena por bytes), feita numa
  thread: ~60 MB num NVMe leva dezenas de ms e o trecho fica protegido da limpeza.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import structlog

from cortes.adapters.recording.segment_timeline import Segment, SegmentTimeline
from cortes.config import RecordingSettings
from cortes.domain.models import RecorderStats, Snapshot

log = structlog.get_logger(__name__)

_LIST_NAME = "segments.csv"
_RELAY_CHUNK = 256 * 1024
_POLL_S = 0.5


class RecorderStalledError(RuntimeError):
    pass


class StreamlinkFfmpegRecorder:
    def __init__(
        self,
        label: str,
        url: str,
        session_dir: Path,
        settings: RecordingSettings,
        *,
        streamlink_args: Sequence[str] = (),
        quality: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._label = label  # Channel.key, só para os logs
        self._url = url
        self._platform_args = list(streamlink_args)
        self._quality = quality or settings.quality
        self._dir = session_dir
        self._s = settings
        self._clock = clock
        window = max(10, int(60 / max(settings.segment_s, 0.5)))
        self._timeline = SegmentTimeline(offset_window=window)
        self._last_segment_at: float | None = None

    # ------------------------------------------------------------------ gravação
    async def run(self) -> None:
        backoff = 2.0
        while True:
            run_id = self._timeline.new_run()
            run_dir = self._dir / f"run{run_id:03d}"
            started = time.monotonic()
            try:
                await self._run_once(run_dir)
                log.info("recorder_stream_ended", channel=self._label, run=run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("recorder_failed", channel=self._label, run=run_id, error=repr(exc))
            if time.monotonic() - started > 120:
                backoff = 2.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _streamlink_args(self) -> list[str]:
        s = self._s
        args = [
            *s.streamlink_cmd(),
            "--stdout",
            "--loglevel", "warning",
            "--hls-live-edge", "2",
            "--stream-segment-threads", "2",
            # O timeout de leitura do próprio streamlink (padrão 60 s) também precisa
            # tolerar os anúncios; quem decide reiniciar é o nosso stall_timeout_s.
            "--stream-timeout", f"{s.stall_timeout_s + 30:g}",
        ]  # fmt: skip
        args += self._platform_args
        args += [self._url, self._quality]
        return args

    def _ffmpeg_args(self) -> list[str]:
        return [
            self._s.ffmpeg_path,
            "-hide_banner", "-nostats", "-loglevel", "warning",
            "-fflags", "+genpts+discardcorrupt",
            "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy",
            "-f", "segment",
            "-segment_time", f"{self._s.segment_s:g}",
            "-segment_format", "mpegts",
            "-segment_list", _LIST_NAME,
            "-segment_list_type", "csv",
            "seg_%08d.ts",
        ]  # fmt: skip

    async def _run_once(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 (instantâneo)
        streamlink = await asyncio.create_subprocess_exec(
            *self._streamlink_args(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            ffmpeg = await asyncio.create_subprocess_exec(
                *self._ffmpeg_args(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                cwd=run_dir,  # nomes relativos: evita escapar caminhos do Windows no FFmpeg
            )
        except BaseException:
            await _terminate(streamlink, 5.0)  # não deixa o streamlink órfão
            raise
        log.info("recorder_started", channel=self._label, dir=str(run_dir))

        list_path = run_dir / _LIST_NAME
        state = _ListReader(list_path, run_dir)
        aux = [
            asyncio.create_task(self._drain(streamlink.stderr, "streamlink")),
            asyncio.create_task(self._drain(ffmpeg.stderr, "ffmpeg")),
            asyncio.create_task(self._watch(state)),
        ]
        try:
            assert streamlink.stdout is not None and ffmpeg.stdin is not None
            await self._relay(streamlink.stdout, ffmpeg.stdin)
        finally:
            await _terminate(streamlink, 5.0)
            await _close_stdin_and_wait(ffmpeg, 10.0)
            for task in aux:
                task.cancel()
            await asyncio.gather(*aux, return_exceptions=True)
            self._ingest(state)  # último segmento, finalizado ao fechar o FFmpeg

    async def _relay(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        stall = self._s.stall_timeout_s
        while True:
            try:
                chunk = await asyncio.wait_for(src.read(_RELAY_CHUNK), timeout=stall)
            except TimeoutError as exc:
                raise RecorderStalledError(f"sem dados há {stall:g}s") from exc
            if not chunk:
                return
            dst.write(chunk)
            await dst.drain()

    async def _drain(self, stream: asyncio.StreamReader | None, name: str) -> None:
        if stream is None:
            return
        async for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if name == "ffmpeg":
                log.debug("ffmpeg_output", channel=self._label, line=line)
            else:
                log.info("streamlink_output", channel=self._label, line=line)

    async def _watch(self, state: _ListReader) -> None:
        while True:
            await asyncio.sleep(_POLL_S)
            self._ingest(state)
            self._expire()

    def _ingest(self, state: _ListReader) -> None:
        now = self._clock()
        for name, pts_start, pts_end in state.read_new():
            self._timeline.observe(state.run_dir / name, pts_start, pts_end, now)
            self._last_segment_at = now

    def stats(self) -> RecorderStats:
        return RecorderStats(
            restarts=max(0, self._timeline.run_id - 1),
            last_segment_at=self._last_segment_at,
            buffered_s=self._timeline.buffered_s,
        )

    def _expire(self) -> None:
        cutoff = self._clock() - self._s.buffer_s
        for segment in self._timeline.expire(cutoff):
            # Windows: arquivo aberto por outro processo falha; a pasta inteira some no cleanup.
            with contextlib.suppress(OSError):
                segment.path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ snapshot
    async def snapshot(self, start_ts: float, end_ts: float, dest: Path) -> Snapshot | None:
        segments = self._timeline.between(start_ts, end_ts)
        if not segments:
            return None
        used = await asyncio.to_thread(_concat, segments, dest)
        if not used:
            return None
        return Snapshot(path=dest, wall_start=used[0].wall_start, wall_end=used[-1].wall_end)

    async def cleanup(self) -> None:
        await asyncio.to_thread(shutil.rmtree, self._dir, True)


class _ListReader:
    """Lê incrementalmente o CSV de segmentos que o FFmpeg vai anexando."""

    def __init__(self, path: Path, run_dir: Path) -> None:
        self.path = path
        self.run_dir = run_dir
        self._pos = 0
        self._pending = ""

    def read_new(self) -> list[tuple[str, float, float]]:
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._pos)
                data = fh.read()
        except FileNotFoundError:
            return []
        if not data:
            return []
        self._pos += len(data)
        *lines, self._pending = (self._pending + data.decode("utf-8", "replace")).split("\n")
        entries: list[tuple[str, float, float]] = []
        for line in lines:
            parts = line.strip().rsplit(",", 2)
            if len(parts) != 3:
                continue
            try:
                entries.append((parts[0], float(parts[1]), float(parts[2])))
            except ValueError:
                continue
        return entries


def _concat(segments: list[Segment], dest: Path) -> list[Segment]:
    # Se algum segmento já foi apagado, usa só o trecho contínuo depois dele.
    usable: list[Segment] = []
    for segment in segments:
        if segment.path.is_file():
            usable.append(segment)
        else:
            usable.clear()
    if not usable:
        return []
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        for segment in usable:
            with segment.path.open("rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)
    return usable


async def _terminate(proc: asyncio.subprocess.Process, timeout_s: float) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def _close_stdin_and_wait(proc: asyncio.subprocess.Process, timeout_s: float) -> None:
    """Fecha o stdin para o FFmpeg finalizar o segmento atual e sair sozinho."""
    if proc.stdin is not None and not proc.stdin.is_closing():
        proc.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await proc.stdin.wait_closed()
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except TimeoutError:
        await _terminate(proc, 2.0)
