from __future__ import annotations

import os
from pathlib import Path

import pytest

from cortes.channel_config import ChannelList, append_channel
from cortes.config import read_channels
from cortes.domain.models import Channel, Platform


def test_append_keeps_the_file_and_validates(tmp_path: Path) -> None:
    path = tmp_path / "channels.yaml"
    original = "channels:\n  - login: fulano  # comentário\n\n# fim\n"
    path.write_text(original, encoding="utf-8", newline="\n")

    channel = Channel(login="Ciclano", platform=Platform.KICK, display_name='A: "b" #c')
    append_channel(path, channel, comment="pelo painel")
    text = path.read_text(encoding="utf-8")
    assert text.startswith(original.rstrip("\n"))
    assert "\r\n" not in text  # arquivo com LF continua com LF
    channels = {c.key: c for c in read_channels(path)}
    assert channels["kick-ciclano"].display_name == 'A: "b" #c'  # aspas e # não quebram o YAML
    assert "twitch-fulano" in channels

    # Repetido: erro e o arquivo fica como estava.
    before = path.read_bytes()
    with pytest.raises(ValueError, match="repetido"):
        append_channel(path, Channel(login="ciclano", platform=Platform.KICK))
    assert path.read_bytes() == before
    assert not (tmp_path / "channels.yaml.novo").exists()


def test_append_to_missing_file_and_refuses_when_list_is_not_last(tmp_path: Path) -> None:
    path = tmp_path / "channels.yaml"
    append_channel(path, Channel(login="fulano"))
    assert [c.key for c in read_channels(path)] == ["twitch-fulano"]

    other = tmp_path / "outro.yaml"
    other.write_text("channels:\n  - login: fulano\nextra: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        append_channel(other, Channel(login="ciclano"))
    assert other.read_text(encoding="utf-8") == "channels:\n  - login: fulano\nextra: 1\n"


def test_channel_list_reload_and_error(tmp_path: Path) -> None:
    path = tmp_path / "channels.yaml"
    path.write_text("channels:\n  - login: fulano\n", encoding="utf-8")
    channels = ChannelList(read_channels(path), path)
    first = channels.loaded_at
    assert [c.key for c in channels.current()] == ["twitch-fulano"]

    path.write_text("channels: [ {login: ", encoding="utf-8")
    os.utime(path, (first + 10, first + 10))  # type: ignore[operator]
    assert [c.key for c in channels.current()] == ["twitch-fulano"]  # mantém a anterior
    assert channels.error and channels.loaded_at == first

    path.write_text("channels:\n  - login: ciclano\n", encoding="utf-8")
    os.utime(path, (first + 20, first + 20))  # type: ignore[operator]
    assert [c.key for c in channels.current()] == ["twitch-ciclano"]
    assert channels.error is None and channels.loaded_at == first + 20  # type: ignore[operator]
