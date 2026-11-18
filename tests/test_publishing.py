from __future__ import annotations

from cortes.domain.models import Channel, Platform
from cortes.domain.publishing import build_description, build_hashtags, suggest_title

CH = Channel(login="fulano", display_name="Fulano", hashtags=("gtarp", "#GTA6"))


def test_title_from_first_sentence() -> None:
    assert suggest_title("Não acredito. Ele caiu", CH, 90) == '"Não acredito." - Fulano'


def test_title_truncates_on_word() -> None:
    title = suggest_title("palavra " * 30, CH, 20)
    assert title.startswith('"palavra palavra') and "…" in title


def test_title_fallback() -> None:
    assert "Fulano" in suggest_title("   ", CH, 90)


def test_hashtags_dedupe_and_prefix() -> None:
    assert build_hashtags(["#gta6", "#twitch"], CH) == ["#gta6", "#twitch", "#gtarp", "#fulano"]


def test_hashtags_with_platform_tags() -> None:
    kick = Channel(login="fulano-gta", platform=Platform.KICK)
    assert build_hashtags(["#gta6"], kick, ["#kick"]) == ["#gta6", "#kick", "#fulanogta"]


def test_youtube_channel_id_is_not_a_hashtag() -> None:
    channel = Channel(login="UC1234567890abcdefGHIJKL", platform=Platform.YOUTUBE)
    assert build_hashtags(["#gta6"], channel) == ["#gta6"]


def test_description_always_has_credit() -> None:
    text = build_description("{title} {hashtags}", title="t", channel=CH, hashtags=["#a"])
    assert "twitch.tv/fulano" in text


def test_description_platform_variable() -> None:
    channel = Channel(login="fulano", platform=Platform.KICK, display_name="Fulano")
    text = build_description(
        "{display_name} na {platform}: {channel_url}", title="t", channel=channel, hashtags=[]
    )
    assert text == "Fulano na Kick: kick.com/fulano"
