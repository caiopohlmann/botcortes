"""config/channels.yaml em uso contínuo: releitura quando muda e inclusão de canais.

O monitor, o worker e o painel releem a lista sozinhos (sem reiniciar) e o painel pode
acrescentar um canal pelo link. O resto do config (settings.yaml) só vale ao reiniciar.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import structlog
import yaml

from cortes.config import read_channels
from cortes.domain.models import Channel

log = structlog.get_logger(__name__)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class ChannelList:
    """Canais de config/channels.yaml, relidos quando o arquivo muda.

    Se o arquivo novo tiver erro (ex.: salvo no meio da edição), continua com a lista
    anterior, guarda o erro em `error` e avisa no log.
    """

    def __init__(
        self,
        channels: Sequence[Channel],
        path: Path,
        load: Callable[[], Sequence[Channel]] | None = None,
        *,
        name: str = "channels",
    ) -> None:
        self._channels = tuple(channels)
        self._path = path
        self._load = load or (lambda: read_channels(path))
        self._name = name  # quem está lendo (aparece no log)
        self._mtime = _mtime(path)  # versão do arquivo já vista
        self._loaded_mtime = self._mtime  # versão do arquivo da lista atual
        self.error: str | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def changed_at(self) -> float | None:
        """Quando channels.yaml foi salvo pela última vez (None = arquivo não existe)."""
        return self._mtime

    @property
    def loaded_at(self) -> float | None:
        """Versão (mtime) do channels.yaml que está na lista atual."""
        return self._loaded_mtime

    def current(self) -> tuple[Channel, ...]:
        mtime = _mtime(self._path)
        with self._lock:
            if mtime is not None and mtime != self._mtime:
                self._mtime = mtime
                try:
                    self._channels = tuple(self._load())
                except Exception as exc:
                    self.error = str(exc)[:300]
                    log.warning(f"{self._name}_reload_failed", error=repr(exc))
                else:
                    self.error = None
                    self._loaded_mtime = mtime
                    log.info(f"{self._name}_reloaded", channels=[c.key for c in self._channels])
            return self._channels


def channel_yaml(channel: Channel, comment: str = "") -> list[str]:
    """Linhas YAML de um canal, no mesmo formato dos canais escritos à mão."""
    # Strings em JSON são YAML válido (aspas duplas): nome com ":" ou "#" não quebra o arquivo.
    lines = [f"  # {comment}"] if comment else []
    lines.append(f"  - login: {json.dumps(channel.login, ensure_ascii=False)}")
    lines.append(f"    platform: {channel.platform.value}")
    if channel.display_name:
        lines.append(f"    display_name: {json.dumps(channel.display_name, ensure_ascii=False)}")
    if channel.language:
        lines.append(f"    language: {channel.language}")
    return lines


_append_lock = threading.Lock()


def append_channel(path: Path, channel: Channel, comment: str = "") -> None:
    """Acrescenta o canal no fim de channels.yaml sem mexer no resto (comentários incluídos).

    Confere o arquivo resultante com as mesmas regras do bot (canal repetido, chave errada)
    antes de trocar o original. Em erro, o original fica como estava (ValueError).
    """
    with _append_lock:
        raw = path.read_bytes() if path.exists() else b""
        text = raw.decode("utf-8")
        newline = "\r\n" if "\r\n" in text else "\n"
        body = text.replace("\r\n", "\n").rstrip("\n")
        if not body:
            body = "channels:"
        block = "\n".join(channel_yaml(channel, comment))
        new_text = f"{body}\n\n{block}\n".replace("\n", newline)

        tmp = path.with_name(f"{path.name}.novo")
        tmp.write_bytes(new_text.encode("utf-8"))
        try:
            try:
                channels = read_channels(tmp)
            except yaml.YAMLError as exc:
                # Ex.: outra chave depois da lista: o bloco novo ficaria no lugar errado.
                raise ValueError(
                    "channels.yaml ficaria inválido; a lista `channels:` precisa ser a "
                    f"última parte do arquivo ({str(exc).splitlines()[0]})"
                ) from exc
            if channel.key not in {c.key for c in channels}:
                # O fim do arquivo não estava dentro da lista "channels:" (outra chave depois).
                raise ValueError(
                    "não consegui acrescentar no fim de channels.yaml: "
                    "a lista `channels:` precisa ser a última parte do arquivo"
                )
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    log.info("channel_added", channel=channel.key, path=str(path))
