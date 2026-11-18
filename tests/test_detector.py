from __future__ import annotations

import pytest

from cortes.domain.detector import ClipParams, DetectorParams, MomentDetector, clip_window
from cortes.domain.models import ChatSignal

PARAMS = DetectorParams(
    trigger_score=50, release_score=20, z_trigger=3, max_episode_s=30, cooldown_s=60
)


def sig(ts: float, score: float, z: float = 6.0, warm: bool = True) -> ChatSignal:
    return ChatSignal(
        ts=ts, rate=10, baseline=2, z=z, hype=0.5, diversity=0.8, score=score, warm=warm
    )


def run(detector: MomentDetector, scores: list[tuple[float, float]]) -> list[float]:
    peaks = []
    for ts, score in scores:
        peak = detector.feed(sig(ts, score))
        if peak:
            peaks.append(peak.peak_ts)
    return peaks


def test_emits_highest_point_of_episode() -> None:
    d = MomentDetector(PARAMS)
    peaks = run(d, [(1, 10), (2, 55), (3, 80), (4, 95), (5, 60), (6, 10)])
    assert peaks == [4]


def test_no_trigger_below_threshold() -> None:
    d = MomentDetector(PARAMS)
    assert run(d, [(t, 49) for t in range(1, 100)]) == []


def test_requires_z_trigger() -> None:
    d = MomentDetector(PARAMS)
    assert d.feed(sig(1, 90, z=2.5)) is None
    assert not d.in_episode


def test_requires_warm_baseline() -> None:
    d = MomentDetector(PARAMS)
    assert d.feed(sig(1, 90, warm=False)) is None
    assert not d.in_episode


def test_cooldown_blocks_duplicate() -> None:
    d = MomentDetector(PARAMS)
    first = run(d, [(1, 90), (2, 10)])
    second = run(d, [(30, 90), (31, 10)])  # dentro do cooldown (60s após o pico em 1)
    third = run(d, [(70, 90), (71, 10)])
    assert first == [1]
    assert second == []
    assert third == [70]


def test_max_episode_duration_closes() -> None:
    d = MomentDetector(PARAMS)
    peaks = run(d, [(float(t), 70.0 + (t == 10) * 20) for t in range(0, 40)])
    assert peaks == [10]


def test_flush_closes_open_episode() -> None:
    d = MomentDetector(PARAMS)
    run(d, [(1, 60), (2, 85)])
    peak = d.flush()
    assert peak is not None and peak.peak_ts == 2
    assert d.flush() is None


def test_invalid_params() -> None:
    with pytest.raises(ValueError):
        DetectorParams(trigger_score=30, release_score=40)


def test_clip_window_accounts_for_reaction() -> None:
    w = clip_window(100.0, ClipParams(pre_roll_s=40, post_roll_s=15, reaction_offset_s=4))
    assert w.event_ts == 96
    assert w.start_ts == 56
    assert w.end_ts == 111
    assert w.duration_s == 55
