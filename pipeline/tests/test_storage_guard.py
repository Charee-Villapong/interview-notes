"""格納ガード（guards.check_note_storage）と保存統合のテスト。

根拠: 本人の仕様決定（2026-11-09）で、保存ガードは「保存先リポジトリの
非公開確認」から「保存先が公開され得ない格納場所であることの git による
機械確認」へ差し替えられた。docs/手順書.md「保存ガード」が正本。

実 git リポジトリ（一時ディレクトリ）で判定そのものを検証する。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SID = "2026-11-09_01"
DATE_DIR = "2026-11-09"
VALID_DRAFT = "下書き（未照合）\n\n# 面談メモ（ダミー）\nダミー本文です。\n"
IGNORE_RULES = "notes/*\n!notes/README.md\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path, *, ignore_rules: bool = True) -> Path:
    """notes/ と README.md を追跡済みにした一時リポジトリを作る。"""
    repo = tmp_path / "repo"
    notes = repo / "notes"
    notes.mkdir(parents=True)
    (notes / "README.md").write_text("# notes\n", encoding="utf-8")
    if ignore_rules:
        (repo / ".gitignore").write_text(IGNORE_RULES, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "notes/README.md")
    if ignore_rules:
        _git(repo, "add", ".gitignore")
    return repo


def _destination(repo: Path) -> Path:
    return repo / "notes" / DATE_DIR / f"{SID}.md"


# ---------------------------------------------------------------------------
# check_note_storage の判定
# ---------------------------------------------------------------------------


def test_check_note_storage_accepts_ignored_destination(tmp_path):
    """正常系: 作業コピー内・ignore 済み・追跡は README のみなら True。"""
    from interview_pipeline.guards import check_note_storage

    repo = _make_repo(tmp_path)

    assert check_note_storage(_destination(repo)) is True


def test_check_note_storage_rejects_when_not_ignored(tmp_path):
    """異常系: ignore ルールが無ければ False（check-ignore で弾く）。"""
    from interview_pipeline.guards import check_note_storage

    repo = _make_repo(tmp_path, ignore_rules=False)

    assert check_note_storage(_destination(repo)) is False


def test_check_note_storage_rejects_when_other_note_tracked(tmp_path):
    """異常系: notes/ 配下に README 以外の追跡ファイルがあれば False。

    保存先自体は ignore 済みのままなので、ls-files 側の条件で弾く。
    """
    from interview_pipeline.guards import check_note_storage

    repo = _make_repo(tmp_path)
    old = repo / "notes" / "2025-01-01"
    old.mkdir(parents=True)
    (old / "old.md").write_text("古いノート\n", encoding="utf-8")
    _git(repo, "add", "-f", "notes/2025-01-01/old.md")

    assert check_note_storage(_destination(repo)) is False


def test_check_note_storage_rejects_when_destination_tracked(tmp_path):
    """異常系: 保存先ファイル自体が追跡されていたら False。"""
    from interview_pipeline.guards import check_note_storage

    repo = _make_repo(tmp_path)
    destination = _destination(repo)
    destination.parent.mkdir(parents=True)
    destination.write_text("既存\n", encoding="utf-8")
    _git(repo, "add", "-f", f"notes/{DATE_DIR}/{SID}.md")

    assert check_note_storage(destination) is False


def test_check_note_storage_rejects_outside_worktree(tmp_path):
    """異常系: 保存先が作業コピーの外なら False。"""
    from interview_pipeline.guards import check_note_storage

    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside" / "notes" / DATE_DIR / f"{SID}.md"
    outside.parent.mkdir(parents=True)

    assert (repo / "notes").is_dir()
    assert check_note_storage(outside) is False


def test_check_note_storage_rejects_when_not_a_repository(tmp_path):
    """異常系: git リポジトリでない場所は確認できないため False。"""
    from interview_pipeline.guards import check_note_storage

    plain = tmp_path / "plain" / "notes"
    plain.mkdir(parents=True)

    assert check_note_storage(plain / DATE_DIR / f"{SID}.md") is False


def test_check_note_storage_rejects_when_git_unavailable(tmp_path, monkeypatch):
    """異常系: git が実行できない環境では確認できないため False。"""
    from interview_pipeline.guards import check_note_storage

    repo = _make_repo(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    assert check_note_storage(_destination(repo)) is False


# ---------------------------------------------------------------------------
# save_draft との統合（実チェッカーを使う）
# ---------------------------------------------------------------------------


def _setup_session(make_store, drive_to, t0):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "awaiting_editing")
    return store


def test_save_draft_with_real_storage_guard_succeeds(
    make_store, drive_to, state_name, t0, tmp_path
):
    """統合正常系: 実ガードで保存でき、ノートは git status に現れない。"""
    from interview_pipeline.guards import check_note_storage
    from interview_pipeline.saving import save_draft

    repo = _make_repo(tmp_path)
    notes_dir = repo / "notes"
    store = _setup_session(make_store, drive_to, t0)
    draft = tmp_path / "draft.md"
    draft.write_text(VALID_DRAFT, encoding="utf-8")

    saved = save_draft(store, SID, draft, notes_dir, storage_checker=check_note_storage)

    assert saved == _destination(repo)
    assert saved.exists()
    assert state_name(store.get(SID).state) == "awaiting_review"
    status = _git(repo, "status", "--porcelain").stdout
    assert SID not in status


def test_save_draft_with_real_storage_guard_refused_without_ignore_rules(
    make_store, drive_to, exc, t0, tmp_path
):
    """統合異常系: ignore ルールを外すと実ガードが保存を止める。"""
    from interview_pipeline.guards import check_note_storage
    from interview_pipeline.saving import save_draft

    repo = _make_repo(tmp_path, ignore_rules=False)
    notes_dir = repo / "notes"
    store = _setup_session(make_store, drive_to, t0)
    draft = tmp_path / "draft.md"
    draft.write_text(VALID_DRAFT, encoding="utf-8")

    with pytest.raises(exc("NoteStorageGuardError")):
        save_draft(store, SID, draft, notes_dir, storage_checker=check_note_storage)

    assert not _destination(repo).exists()
