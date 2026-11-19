"""Navegação nas pastas de cortes (data/clips) para o painel, como um explorador de arquivos.

Mostra as pastas reais do disco: se você mover ou apagar um corte no Windows, o painel
reflete. Os dados de cada corte (nota, legenda, motivos) vêm do banco, pelo caminho do
arquivo (ou pelo id no nome, nos cortes do formato antigo). Nunca sai de `root`
(caminhos com ".." ou absolutos são recusados).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cortes.domain.models import clip_number

README_NAME = "_LEIA-ME.txt"
VIDEO_SUFFIX = ".mp4"


class BadPathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FolderItem:
    name: str
    rel: str  # caminho relativo a root, com "/"
    clips: int  # quantos .mp4 existem dentro (recursivo)
    grade: str | None  # boa | revisar | ruim, se for uma pasta de qualidade


@dataclass(frozen=True, slots=True)
class FileItem:
    name: str
    rel: str
    clip_id: str  # id pelo nome (formato antigo); o painel troca pelo do banco, se achar
    path: Path  # caminho absoluto (é como o banco guarda o vídeo do corte)
    size_mb: float
    description_rel: str | None  # o .txt ao lado, se existir


@dataclass(frozen=True, slots=True)
class Listing:
    rel: str
    crumbs: list[tuple[str, str]]  # (nome, rel) do topo até aqui
    folders: list[FolderItem] = field(default_factory=list)
    files: list[FileItem] = field(default_factory=list)
    readme: str | None = None
    grade: str | None = None  # qualidade da pasta atual, se for uma


def clip_id_from_name(name: str) -> str:
    """'20261118-213045-twitch-fulano-a3f9_nota72.mp4' -> '20261118-213045-twitch-fulano-a3f9'."""
    stem = name.removesuffix(VIDEO_SUFFIX)
    return stem.rsplit("_nota", 1)[0] if "_nota" in stem else stem


def resolve(root: Path, rel: str) -> Path:
    """Caminho seguro dentro de root (recusa '..', absolutos e links para fora)."""
    rel = (rel or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p]
    if any(p in (".", "..") or ":" in p for p in parts):
        raise BadPathError("caminho inválido")
    base = root.resolve()
    target = base.joinpath(*parts).resolve()
    if not target.is_relative_to(base):
        raise BadPathError("caminho fora da pasta de cortes")
    return target


def delete_clip_files(root: Path, rel: str) -> Path:
    """Apaga o .mp4 de `rel` e o .txt ao lado. Devolve o caminho do vídeo apagado.

    Só aceita um .mp4 dentro de root. O vídeo vai primeiro: se ele estiver aberto em
    outro programa (o Windows recusa), nada é apagado e o OSError sobe.
    """
    video = resolve(root, rel)
    if video.suffix.lower() != VIDEO_SUFFIX or not video.is_file():
        raise BadPathError("corte não encontrado")
    video.unlink()
    video.with_suffix(".txt").unlink(missing_ok=True)
    return video


def _count_videos(folder: Path) -> int:
    try:
        return sum(1 for _ in folder.rglob(f"*{VIDEO_SUFFIX}"))
    except OSError:
        return 0


def list_folder(root: Path, rel: str, grade_by_folder: Mapping[str, str] | None = None) -> Listing:
    grade_by_folder = grade_by_folder or {}
    folder = resolve(root, rel)
    if not folder.is_dir():
        raise BadPathError("pasta não encontrada")
    base = root.resolve()
    rel_norm = folder.relative_to(base).as_posix() if folder != base else ""

    crumbs = [("Cortes", "")]
    acc: list[str] = []
    for part in [p for p in rel_norm.split("/") if p]:
        acc.append(part)
        crumbs.append((part, "/".join(acc)))

    folders: list[FolderItem] = []
    files: list[FileItem] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        entry_rel = f"{rel_norm}/{entry.name}".strip("/")
        if entry.is_dir():
            folders.append(
                FolderItem(
                    name=entry.name,
                    rel=entry_rel,
                    clips=_count_videos(entry),
                    grade=grade_by_folder.get(entry.name),
                )
            )
        elif entry.suffix.lower() == VIDEO_SUFFIX:
            txt = entry.with_suffix(".txt")
            try:
                size_mb = entry.stat().st_size / 1024**2
            except OSError:
                size_mb = 0.0
            files.append(
                FileItem(
                    name=entry.name,
                    rel=entry_rel,
                    clip_id=clip_id_from_name(entry.name),
                    path=entry,
                    size_mb=size_mb,
                    description_rel=f"{rel_norm}/{txt.name}".strip("/") if txt.is_file() else None,
                )
            )
    # Mais novo primeiro: número do corte (10 antes de 9) e, no formato antigo, a data no nome.
    files.sort(key=lambda f: (clip_number(f.name) or 0, f.name), reverse=True)

    readme_path = folder / README_NAME
    readme = None
    if readme_path.is_file():
        try:
            readme = readme_path.read_text(encoding="utf-8").strip()
        except OSError:
            readme = None
    return Listing(
        rel=rel_norm,
        crumbs=crumbs,
        folders=folders,
        files=files,
        readme=readme,
        grade=grade_by_folder.get(folder.name),
    )
