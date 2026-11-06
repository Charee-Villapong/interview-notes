"""下書き保存（save_draft）のテスト。

根拠: pipeline/SPEC.md の ``saving.py`` 契約、docs/状態管理.md
「遷移条件」（awaiting_editing → draft_saved は保存ガード通過が必須）、
docs/手順書.md「保存ガード」（音声非含有・保存先の非公開確認・
冒頭に「下書き（未照合）」明示）。

前提（解釈）: 保存先の日付フォルダはセッションの日付（セッションIDの
日付部分＝録音日）で、配置先は ``notes_dir/<日付>/<session_id>.md``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

SID = "2026-11-09_01"
DATE_DIR = "2026-11-09"
VALID_DRAFT = "下書き（未照合）\n\n# 面談メモ（ダミー）\nダミー本文です。\n"


def _setup(make_store, drive_to, t0, tmp_path):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "awaiting_editing")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    return store, notes_dir


def _write_draft(tmp_path, content=VALID_DRAFT, name="draft.md") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _saved_files(notes_dir: Path) -> list[Path]:
    return sorted(p for p in notes_dir.rglob("*") if p.is_file())


def _save(store, draft_path, notes_dir, privacy_checker):
    from interview_pipeline.saving import save_draft

    return save_draft(
        store, SID, draft_path, notes_dir, privacy_checker=privacy_checker
    )


def test_save_draft_success_places_file_and_advances_state(
    make_store, drive_to, state_name, t0, tmp_path
):
    """正常系: 日付フォルダへ配置され draft_saved → awaiting_review へ進む。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    draft = _write_draft(tmp_path)

    saved = _save(store, draft, notes_dir, privacy_checker=lambda: True)

    assert isinstance(saved, Path)
    assert saved.exists()
    assert saved.parent.name == DATE_DIR
    assert saved.name == f"{SID}.md"
    assert notes_dir in saved.parents
    assert saved.read_text(encoding="utf-8") == VALID_DRAFT

    session = store.get(SID)
    assert state_name(session.state) == "awaiting_review"
    assert session.draft_saved_at is not None


def test_save_draft_with_audio_draft_aborts(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """保存対象が音声ファイルなら保存中止＋例外＋needs_reprocess。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    audio_draft = tmp_path / "draft.wav"
    audio_draft.write_bytes(b"dummy-audio")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio_draft, notes_dir, privacy_checker=lambda: True)

    assert _saved_files(notes_dir) == []
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "awaiting_editing"
    assert session.error_kind == "audio_content_detected"
    assert session.draft_saved_at is None


def test_save_draft_with_uppercase_audio_extension_aborts(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """大文字の音声・動画拡張子でも保存中止になる。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    audio_draft = tmp_path / "DRAFT.MP4"
    audio_draft.write_bytes(b"dummy-video")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio_draft, notes_dir, privacy_checker=lambda: True)

    assert _saved_files(notes_dir) == []
    assert state_name(store.get(SID).state) == "needs_reprocess"


@pytest.mark.parametrize(
    "filename", ["draft.m4a", "draft.mov", "draft.mp3", "draft.webm"]
)
def test_save_draft_rejects_various_audio_extensions(
    make_store, drive_to, exc, t0, tmp_path, filename
):
    """手順書列挙の拡張子はいずれも保存対象にできない。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    audio_draft = tmp_path / filename
    audio_draft.write_bytes(b"dummy")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio_draft, notes_dir, privacy_checker=lambda: True)
    assert _saved_files(notes_dir) == []


def test_save_draft_refused_when_repository_not_private(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """保存先が非公開でない場合は保存しない（例外）。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    draft = _write_draft(tmp_path)

    with pytest.raises(exc("RepositoryNotPrivateError")):
        _save(store, draft, notes_dir, privacy_checker=lambda: False)

    assert _saved_files(notes_dir) == []
    session = store.get(SID)
    assert session.draft_saved_at is None
    assert state_name(session.state) != "awaiting_review"


def test_save_draft_without_draft_marking_rejected(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """冒頭に「下書き（未照合）」表記のない md は保存できない。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    draft = _write_draft(tmp_path, content="# 面談メモ\n表記のない本文。\n")

    with pytest.raises(exc("DraftMarkingError")):
        _save(store, draft, notes_dir, privacy_checker=lambda: True)

    assert _saved_files(notes_dir) == []
    session = store.get(SID)
    assert session.draft_saved_at is None
    assert state_name(session.state) != "awaiting_review"


def test_save_draft_marking_must_be_at_top(
    make_store, drive_to, exc, t0, tmp_path
):
    """表記が本文の途中にあるだけでは不十分（冒頭にあること）。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    content = "# 面談メモ\n\n本文の途中に 下書き（未照合） と書いただけ。\n"
    draft = _write_draft(tmp_path, content=content)

    with pytest.raises(exc("DraftMarkingError")):
        _save(store, draft, notes_dir, privacy_checker=lambda: True)

    assert _saved_files(notes_dir) == []
