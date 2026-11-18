from __future__ import annotations

from pathlib import Path

import pytest

from cortes.config import Secrets, Settings, load_settings
from cortes.domain.models import Channel, ChatRef, Platform

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def secrets_for(config_dir: Path, profile: str = "prod") -> Secrets:
    return Secrets(_env_file=None, cortes_config_dir=config_dir, cortes_profile=profile)


def test_repo_config_loads() -> None:
    for profile in ("prod", "dev"):
        settings = load_settings(secrets_for(REPO_CONFIG, profile))
        assert settings.channels
        assert settings.paths.data_dir.is_absolute()
        assert settings.detection.hype_tokens["😂"] == pytest.approx(0.8)
        assert settings.publish.platform_hashtags[Platform.KICK] == ["#kick"]


def write_channels(tmp_path: Path, body: str) -> Path:
    (tmp_path / "channels.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def test_channels_with_platforms_and_extra_chats(tmp_path: Path) -> None:
    write_channels(
        tmp_path,
        """
channels:
  - login: Fulano
    extra_chats:
      - {platform: kick, login: fulano}
      - {platform: youtube, login: "@FulanoYT"}
  - login: ciclano
    platform: kick
    chat_id: "668"
""",
    )
    settings = load_settings(secrets_for(tmp_path))
    first, second = settings.channels
    assert first.key == "twitch-fulano"
    assert first.extra_chats == (
        ChatRef(Platform.KICK, "fulano"),
        ChatRef(Platform.YOUTUBE, "fulanoyt"),
    )
    assert second.key == "kick-ciclano" and second.chat_id == "668"


def test_extra_chat_also_listed_as_channel_is_rejected(tmp_path: Path) -> None:
    write_channels(
        tmp_path,
        """
channels:
  - login: fulano
    extra_chats: [{platform: kick, login: fulano}]
  - login: fulano
    platform: kick
""",
    )
    with pytest.raises(ValueError, match="extra_chats"):
        load_settings(secrets_for(tmp_path))


def test_duplicate_channel_is_rejected(tmp_path: Path) -> None:
    write_channels(tmp_path, "channels:\n  - login: a\n  - login: A\n")
    with pytest.raises(ValueError, match="repetido"):
        load_settings(secrets_for(tmp_path))


def test_channel_lookup() -> None:
    settings = Settings(
        channels=[Channel(login="fulano"), Channel(login="ciclano", platform=Platform.KICK)]
    )
    assert settings.channel("twitch-fulano").platform is Platform.TWITCH
    assert settings.channel("ciclano").platform is Platform.KICK  # login único
    unknown = settings.channel("youtube-NovoCanal")
    assert unknown.platform is Platform.YOUTUBE and unknown.login == "novocanal"
    assert settings.channel("outro").key == "twitch-outro"


def test_streamlink_args_per_platform() -> None:
    settings = Settings()
    twitch = settings.streamlink_args(Platform.TWITCH)
    assert "--twitch-disable-ads" in twitch and "--twitch-low-latency" in twitch
    assert not any(a.startswith("--twitch") for a in settings.streamlink_args(Platform.KICK))
    assert settings.chat_delay_s(Platform.YOUTUBE) > settings.chat_delay_s(Platform.TWITCH)
