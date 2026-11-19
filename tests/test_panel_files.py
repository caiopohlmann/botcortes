from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cortes.panel.files import (
    BadPathError,
    clip_id_from_name,
    delete_clip_files,
    list_folder,
    resolve,
)

GRADES = {"1-legenda-boa": "boa", "2-legenda-revisar": "revisar", "3-legenda-ruim": "ruim"}


def make_tree() -> Path:
    root = Path(tempfile.mkdtemp(prefix="cortes-clips-"))
    good = root / "pending" / "Fulano" / "1-legenda-boa"
    review = root / "pending" / "Fulano" / "2-legenda-revisar"
    for folder in (good, review, root / "pending" / "Ciclano" / "1-legenda-boa"):
        folder.mkdir(parents=True)
    (good / "20261118-210000-twitch-fulano-aaaa_nota80.mp4").write_bytes(b"x" * 1024)
    (good / "20261118-210000-twitch-fulano-aaaa_nota80.txt").write_text("desc", encoding="utf-8")
    (good / "20261118-220000-twitch-fulano-bbbb_nota70.mp4").write_bytes(b"x")
    (good / "_LEIA-ME.txt").write_text("LEGENDA BOA\n\nexplicação", encoding="utf-8")
    (review / "20261118-230000-kick-fulano-cccc_nota60.mp4").write_bytes(b"x")
    (root / "pending" / "Ciclano" / "1-legenda-boa" / "x_nota50.mp4").write_bytes(b"x")
    return root


def test_clip_id_from_name() -> None:
    assert clip_id_from_name("20261118-210000-twitch-fulano-aaaa_nota80.mp4") == (
        "20261118-210000-twitch-fulano-aaaa"
    )
    assert clip_id_from_name("sem-nota.mp4") == "sem-nota"


def test_root_and_creator_listing() -> None:
    root = make_tree()
    top = list_folder(root, "", GRADES)
    assert [f.name for f in top.folders] == ["pending"] and top.folders[0].clips == 4
    assert top.crumbs == [("Cortes", "")]

    pending = list_folder(root, "pending", GRADES)
    assert [(f.name, f.clips) for f in pending.folders] == [("Ciclano", 1), ("Fulano", 3)]

    fulano = list_folder(root, "pending/Fulano", GRADES)
    assert [(f.name, f.grade, f.clips) for f in fulano.folders] == [
        ("1-legenda-boa", "boa", 2),
        ("2-legenda-revisar", "revisar", 1),
    ]
    assert fulano.crumbs[-1] == ("Fulano", "pending/Fulano")


def test_quality_folder_lists_videos_newest_first_with_readme() -> None:
    root = make_tree()
    listing = list_folder(root, "pending/Fulano/1-legenda-boa", GRADES)
    assert listing.grade == "boa"
    assert listing.readme is not None and listing.readme.startswith("LEGENDA BOA")
    assert [f.clip_id for f in listing.files] == [
        "20261118-220000-twitch-fulano-bbbb",
        "20261118-210000-twitch-fulano-aaaa",
    ]
    older = listing.files[1]
    assert older.description_rel == (
        "pending/Fulano/1-legenda-boa/20261118-210000-twitch-fulano-aaaa_nota80.txt"
    )
    assert listing.files[0].description_rel is None


def test_paths_cannot_escape_root() -> None:
    root = make_tree()
    for bad in ("..", "pending/../..", "../segredo", "C:/Windows", "pending/..\\.."):
        with pytest.raises(BadPathError):
            resolve(root, bad)
    with pytest.raises(BadPathError):
        list_folder(root, "pending/NaoExiste", GRADES)
    assert resolve(root, "/pending/Fulano/") == (root / "pending" / "Fulano").resolve()


def test_delete_clip_files_removes_video_and_description() -> None:
    root = make_tree()
    folder = root / "pending" / "Fulano" / "1-legenda-boa"
    rel = "pending/Fulano/1-legenda-boa/20261118-210000-twitch-fulano-aaaa_nota80.mp4"
    deleted = delete_clip_files(root, rel)
    assert deleted.name == "20261118-210000-twitch-fulano-aaaa_nota80.mp4"
    assert not (folder / "20261118-210000-twitch-fulano-aaaa_nota80.mp4").exists()
    assert not (folder / "20261118-210000-twitch-fulano-aaaa_nota80.txt").exists()
    # o resto da pasta fica intacto
    assert (folder / "20261118-220000-twitch-fulano-bbbb_nota70.mp4").exists()
    assert (folder / "_LEIA-ME.txt").exists()


def test_delete_clip_files_refuses_non_videos_and_escapes() -> None:
    root = make_tree()
    for rel in (
        "pending/Fulano/1-legenda-boa/_LEIA-ME.txt",
        "pending/Fulano/1-legenda-boa/nao-existe_nota10.mp4",
        "pending/Fulano",
        "../fora.mp4",
    ):
        with pytest.raises(BadPathError):
            delete_clip_files(root, rel)
    assert (root / "pending" / "Fulano" / "1-legenda-boa" / "_LEIA-ME.txt").exists()
