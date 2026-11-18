from __future__ import annotations

import pytest

from cortes.domain.models import ChatSignal
from cortes.domain.scoring import (
    ChatAnalyzer,
    HypeMatcher,
    HypeRule,
    ScoreWeights,
    ScoringParams,
    combine_scores,
)
from tests.conftest import msg

PARAMS = ScoringParams(
    window_s=5.0,
    baseline_half_life_s=300.0,
    warmup_s=60.0,
    hype_tokens={"KEKW": 1.0, "LUL": 0.5},
    hype_patterns=(HypeRule(pattern="k{4,}", weight=0.9),),
)


def feed_steady(
    analyzer: ChatAnalyzer, start: float, end: float, per_second: int, text: str = "oi"
) -> list[ChatSignal]:
    """Simula `per_second` msgs/s de autores distintos e avalia a cada segundo."""
    signals = []
    t = start
    n = 0
    while t < end:
        for i in range(per_second):
            analyzer.add(msg(t + i / per_second, author=f"u{n}", text=text))
            n += 1
        t += 1.0
        signals.append(analyzer.evaluate(t))
    return signals


class TestHypeMatcher:
    def test_exact_token_case_insensitive(self) -> None:
        m = HypeMatcher({"KEKW": 1.0}, ())
        assert m.score("kekw") == 1.0
        assert m.score("KEKW!!") == 1.0

    def test_repeated_token_counts_once(self) -> None:
        m = HypeMatcher({"LUL": 0.3}, ())
        assert m.score("LUL LUL LUL LUL") == pytest.approx(0.3)

    def test_distinct_tokens_sum_and_cap(self) -> None:
        m = HypeMatcher({"LUL": 0.6, "KEKW": 0.6}, ())
        assert m.score("LUL KEKW") == 1.0

    def test_regex_pattern(self) -> None:
        m = HypeMatcher({}, (HypeRule(pattern="k{4,}", weight=0.9),))
        assert m.score("KKKKKKK") == pytest.approx(0.9)
        assert m.score("kkk") == 0.0

    def test_plain_text_scores_zero(self) -> None:
        m = HypeMatcher({"KEKW": 1.0}, ())
        assert m.score("boa noite chat") == 0.0


class TestChatAnalyzer:
    def test_no_score_during_warmup(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        signals = feed_steady(analyzer, 0, 30, per_second=20, text="KEKW")
        assert all(not s.warm for s in signals)
        assert all(s.score == 0.0 for s in signals)

    def test_steady_chat_has_low_z(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        signals = feed_steady(analyzer, 0, 200, per_second=4)
        tail = signals[-60:]
        assert all(s.warm for s in tail)
        assert max(abs(s.z) for s in tail) < 1.0
        assert max(s.score for s in tail) == 0.0
        assert analyzer.baseline == pytest.approx(4.0, rel=0.1)

    def test_baseline_converges_fast_on_big_chat(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        feed_steady(analyzer, 0, 61, per_second=50)
        assert analyzer.baseline == pytest.approx(50.0, rel=0.15)

    def test_spike_with_hype_scores_high(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        feed_steady(analyzer, 0, 200, per_second=3)
        spike = feed_steady(analyzer, 200, 208, per_second=30, text="KEKW KKKKK")
        best = max(spike, key=lambda s: s.score)
        assert best.z > 5
        assert best.score > 70
        # Com a janela inteira no pico, toda mensagem é hype.
        assert spike[-1].hype == pytest.approx(1.0)

    def test_spike_without_hype_scores_lower(self) -> None:
        a1, a2 = ChatAnalyzer(PARAMS), ChatAnalyzer(PARAMS)
        feed_steady(a1, 0, 200, per_second=3)
        feed_steady(a2, 0, 200, per_second=3)
        hype = max(s.score for s in feed_steady(a1, 200, 208, per_second=30, text="KEKW"))
        plain = max(s.score for s in feed_steady(a2, 200, 208, per_second=30, text="oi"))
        assert hype > plain > 0

    def test_single_spammer_scores_lower_than_crowd(self) -> None:
        crowd, spam = ChatAnalyzer(PARAMS), ChatAnalyzer(PARAMS)
        feed_steady(crowd, 0, 200, per_second=3)
        feed_steady(spam, 0, 200, per_second=3)
        crowd_best = max(s.score for s in feed_steady(crowd, 200, 208, per_second=30))
        t = 200.0
        spam_scores = []
        while t < 208:
            for i in range(30):
                spam.add(msg(t + i / 30, author="spammer", text="oi"))
            t += 1
            spam_scores.append(spam.evaluate(t).score)
        assert crowd_best > max(spam_scores)

    def test_spike_does_not_poison_baseline(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        feed_steady(analyzer, 0, 200, per_second=3)
        before = analyzer.baseline
        feed_steady(analyzer, 200, 230, per_second=60)
        assert analyzer.baseline < before * 1.5

    def test_dead_chat_never_scores(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        signals = []
        for t in range(1, 300):
            if t % 20 == 0:
                analyzer.add(msg(float(t), author=f"u{t}", text="KEKW"))
            signals.append(analyzer.evaluate(float(t)))
        assert max(s.score for s in signals) == 0.0

    def test_chat_outage_does_not_create_false_peak(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        feed_steady(analyzer, 0, 200, per_second=10)
        before = analyzer.baseline
        for t in range(201, 2000):  # ~30 min sem mensagens (chat caiu)
            analyzer.evaluate(float(t))
        after = feed_steady(analyzer, 2000, 2020, per_second=10)
        assert max(s.score for s in after) == 0.0
        assert analyzer.baseline > before * 0.8

    def test_small_chat_silence_still_updates_baseline(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        for t in range(1, 200):  # 1 msg a cada 4 s: 30 s de silêncio = só ~7 esperadas
            if t % 4 == 0:
                analyzer.add(msg(float(t), author=f"u{t}"))
            analyzer.evaluate(float(t))
        before = analyzer.baseline
        for t in range(201, 800):
            analyzer.evaluate(float(t))
        assert analyzer.baseline < before * 0.5

    def test_messages_ahead_of_evaluation_wait_their_turn(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        analyzer.add(msg(105.0, author="a"))
        assert analyzer.evaluate(100.0).rate == 0.0
        assert analyzer.evaluate(106.0).rate == pytest.approx(1 / PARAMS.window_s)

    def test_out_of_order_old_message_ignored(self) -> None:
        analyzer = ChatAnalyzer(PARAMS)
        analyzer.add(msg(99.0, author="a"))
        analyzer.add(msg(80.0, author="b"))  # chegou atrasado e fora da janela
        assert analyzer.evaluate(100.0).rate == pytest.approx(1 / PARAMS.window_s)

    def test_invalid_weights(self) -> None:
        with pytest.raises(ValueError):
            ChatAnalyzer(ScoringParams(weight_rate=0, weight_hype=0, weight_diversity=0))


class TestCombineScores:
    def test_weighted(self) -> None:
        w = ScoreWeights(chat_weight=0.7, audio_weight=0.3)
        assert combine_scores(100, 1.0, w) == pytest.approx(100)
        assert combine_scores(100, 0.0, w) == pytest.approx(70)
        assert combine_scores(0, 1.0, w) == pytest.approx(30)

    def test_clamps(self) -> None:
        w = ScoreWeights()
        assert combine_scores(250, 3.0, w) == pytest.approx(100)
        assert combine_scores(-5, -1.0, w) == pytest.approx(0)
