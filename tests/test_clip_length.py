from __future__ import annotations

from cortes.domain.clip_length import (
    ShortClipParams,
    choose_cut,
    cut_transcript,
    length_label,
)
from cortes.domain.models import Transcript, Word

P = ShortClipParams()
WINDOW = 61.0
EVENT = 43.0  # o lance cai a 43 s do início do trecho capturado (pre_roll)


def speech(*spans: tuple[float, float]) -> list[Word]:
    """Uma palavra a cada 0,4 s dentro de cada trecho de fala."""
    words: list[Word] = []
    for start, end in spans:
        t = start
        while t + 0.3 <= end:
            words.append(Word(f"w{len(words)}", round(t, 2), round(t + 0.3, 2), 0.9))
            t += 0.4
    return words


def test_quick_play_with_pauses_becomes_30s() -> None:
    # Fala em blocos, com silêncio perto de 21 s (início do curto) e de 51 s (fim).
    words = speech((5, 19.5), (22.5, 49), (52, 60))
    cut = choose_cut(words, EVENT, WINDOW, hype_s=6, params=P)
    assert cut.short and "lance rápido" in cut.reason
    assert 17 <= cut.offset_s <= 25 and 28 <= cut.duration_s <= 34
    assert cut.offset_s <= EVENT - 15  # o lance não fica no começo do corte
    assert cut.offset_s + cut.duration_s >= EVENT + 5  # sobra reação depois do lance


def test_long_hype_or_continuous_speech_keeps_the_minute() -> None:
    words = speech((5, 19.5), (22.5, 49), (52, 60))
    long_hype = choose_cut(words, EVENT, WINDOW, hype_s=25, params=P)
    assert not long_hype.short and long_hype.duration_s == WINDOW
    assert "empolgação longa" in long_hype.reason

    nonstop = choose_cut(speech((0, 61)), EVENT, WINDOW, hype_s=5, params=P)
    assert not nonstop.short and "fala contínua" in nonstop.reason

    old_moment = choose_cut(words, EVENT, WINDOW, hype_s=None, params=P)
    assert not old_moment.short and old_moment.duration_s == WINDOW


def test_no_speech_is_easy_to_cut_and_short_window_is_kept() -> None:
    cut = choose_cut([], EVENT, WINDOW, hype_s=4, params=P)
    assert cut.short and cut.duration_s == 30.0 and cut.offset_s == EVENT - 22

    small = choose_cut([], 20, 30, hype_s=4, params=P)  # a live acabou: só 30 s gravados
    assert not small.short and small.duration_s == 30 and "só havia 30 s" in small.reason

    off = choose_cut([], EVENT, WINDOW, hype_s=4, params=ShortClipParams(enabled=False))
    assert not off.short and off.duration_s == WINDOW


def test_cut_transcript_shifts_times_and_drops_words_outside() -> None:
    words = (Word("antes", 1, 1.5), Word("olha", 22, 22.4), Word("isso", 30, 30.5))
    transcript = Transcript(language="pt", text="antes olha isso", words=words)
    cut = choose_cut(list(words), EVENT, WINDOW, hype_s=4, params=P)
    part = cut_transcript(transcript, cut)
    assert [w.text for w in part.words] == ["olha", "isso"] and part.text == "olha isso"
    assert part.words[0].start == round(22 - cut.offset_s, 3)


def test_length_label() -> None:
    assert length_label(30.2) == "(30s)"
    assert length_label(45) == "(45s)"
    assert length_label(61) == "" and length_label(59.6) == ""
