from __future__ import annotations

import pytest

from cortes.domain.models import Channel, ChatRef, Platform, channel_key


def test_twitch_is_default_and_normalized() -> None:
    channel = Channel(login="  Fulano ")
    assert channel.platform is Platform.TWITCH
    assert channel.login == "fulano"
    assert channel.key == "twitch-fulano"
    assert channel.url == "twitch.tv/fulano"
    assert channel.watch_url == "https://twitch.tv/fulano"


def test_kick_urls() -> None:
    channel = Channel(login="Fulano-GTA", platform=Platform.KICK)
    assert channel.key == "kick-fulano-gta"
    assert channel.url == "kick.com/fulano-gta"
    assert channel.watch_url == "https://kick.com/fulano-gta"


def test_youtube_handle_and_channel_id() -> None:
    handle = Channel(login="@CanalTop", platform=Platform.YOUTUBE)
    assert handle.login == "canaltop"
    assert handle.url == "youtube.com/@canaltop"
    assert handle.watch_url == "https://www.youtube.com/@canaltop/live"

    channel_id = "UC1234567890abcdefGHIJKL"
    by_id = Channel(login=channel_id, platform=Platform.YOUTUBE)
    assert by_id.login == channel_id  # ID diferencia maiúsculas: não normaliza
    assert by_id.url == f"youtube.com/channel/{channel_id}"


def test_same_login_on_two_platforms_has_distinct_keys() -> None:
    assert Channel(login="x").key != Channel(login="x", platform=Platform.KICK).key


def test_key_is_safe_for_windows_filenames() -> None:
    key = Channel(login="fulano", platform=Platform.YOUTUBE).key
    assert not any(ch in key for ch in '<>:"/\\|?*')


def test_chat_ref_normalizes_login() -> None:
    ref = ChatRef(platform=Platform.KICK, login="@Fulano")
    assert ref.login == "fulano"
    assert channel_key(ref.platform, ref.login) == "kick-fulano"


def test_folder_name_uses_display_name_and_is_windows_safe() -> None:
    assert Channel(login="fulano", display_name="Fulano GTA").folder_name == "Fulano GTA"
    assert Channel(login="fulano").folder_name == "fulano"
    weird = Channel(login="x", display_name="Fu:la/no? <RP> *top*.")
    assert weird.folder_name == "Fu_la_no_ _RP_ _top_"
    assert Channel(login="con").folder_name == "con_"  # nome reservado no Windows
    assert Channel(login="x", display_name="???").folder_name == "twitch-x"  # só símbolos


def test_folder_override_and_same_creator_on_two_platforms() -> None:
    twitch = Channel(login="fulano", display_name="Fulano")
    kick = Channel(login="fulano", platform=Platform.KICK, display_name="Fulano")
    assert twitch.folder_name == kick.folder_name == "Fulano"
    custom = Channel(login="fulano", folder="Fulano (cortes)")
    assert custom.folder_name == "Fulano (cortes)"


def test_parse_channel_link() -> None:
    from cortes.domain.models import parse_channel_link

    assert parse_channel_link("https://www.twitch.tv/Alanzoka") == (Platform.TWITCH, "alanzoka")
    assert parse_channel_link("twitch.tv/alanzoka/videos?filter=all") == (
        Platform.TWITCH,
        "alanzoka",
    )
    assert parse_channel_link("m.twitch.tv/alanzoka") == (Platform.TWITCH, "alanzoka")
    assert parse_channel_link("https://kick.com/Gabe-Peixe") == (Platform.KICK, "gabe-peixe")
    assert parse_channel_link(" kick.com/gabepeixe/ ") == (Platform.KICK, "gabepeixe")
    assert parse_channel_link("https://www.youtube.com/@PaulinhoLOKOoficial/live") == (
        Platform.YOUTUBE,
        "paulinholokooficial",
    )
    channel_id = "UC7c0msgggkLeIW6lwJAy-Ow"
    assert parse_channel_link(f"youtube.com/channel/{channel_id}") == (Platform.YOUTUBE, channel_id)

    for bad in (
        "",
        "alanzoka",  # sem site: não dá para saber a plataforma
        "https://instagram.com/fulano",
        "twitch.tv",
        "twitch.tv/directory/category/gta",
        "kick.com/categories",
        "youtube.com/watch?v=abc",
        "youtube.com/c/fulano",
        "youtu.be/abc",
        "twitch.tv/a",  # curto demais
        "kick.com/fu%20lano",
    ):
        with pytest.raises(ValueError):
            parse_channel_link(bad)
