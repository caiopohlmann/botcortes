"""Empolgação pelo áudio: quanto o volume perto do lance sobe acima do normal do trecho.

Vetorizado em numpy: ~55 s de áudio a 16 kHz (880 mil amostras) leva ~1 ms.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from cortes.config import AudioSettings

_FRAME_S = 0.1
_SMOOTH_FRAMES = 10  # média móvel de 1 s
_FLOOR_DB = -70.0


def loudness_db(pcm16: bytes, sample_rate: int) -> npt.NDArray[np.float64]:
    """RMS em dBFS por quadro de 100 ms, suavizado em 1 s."""
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64) / 32768.0
    frame = int(sample_rate * _FRAME_S)
    n = samples.size // frame
    if n == 0:
        return np.empty(0, dtype=np.float64)
    frames = samples[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    raw_db = np.maximum(20.0 * np.log10(rms), _FLOOR_DB).astype(np.float64)
    if n < _SMOOTH_FRAMES:
        return raw_db
    kernel = np.full(_SMOOTH_FRAMES, 1.0 / _SMOOTH_FRAMES, dtype=np.float64)
    # Padding com o valor da borda: zeros (0 dB = volume máximo) criariam picos falsos.
    left = _SMOOTH_FRAMES // 2
    padded = np.pad(raw_db, (left, _SMOOTH_FRAMES - 1 - left), mode="edge")
    return np.convolve(padded, kernel, mode="valid").astype(np.float64)


def audio_excitement(pcm16: bytes, sample_rate: int, event_s: float, s: AudioSettings) -> float:
    db = loudness_db(pcm16, sample_rate)
    if db.size < 20:
        return 0.0
    baseline = float(np.median(db))
    lo = max(0, int((event_s - s.before_event_s) / _FRAME_S))
    hi = min(db.size, int((event_s + s.after_event_s) / _FRAME_S))
    region = db[lo:hi] if hi > lo else db
    peak = float(region.max())
    return float(np.clip((peak - baseline) / s.range_db, 0.0, 1.0))
