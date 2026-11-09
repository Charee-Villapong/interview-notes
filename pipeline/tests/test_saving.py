"""要約の登録（save_note）のテスト。

根拠: pipeline/SPEC.md の ``saving.py`` 契約、docs/状態管理.md
「遷移条件」（awaiting_editing → draft_saved は音声非含有チェック
通過が必須）。要約の実体は作業領域に一時保持したままで、登録は
コピーを作らない。非公開リポジトリへの push は publish 側の担当。
"""

from __future__ import annotations

from pathlib import Path

import pytest

SID = "2026-11-09_01"
VALID_NOTE = "# 面談ノート（ダミー）\nダミー本文です。\n"


def _setup(make_store, drive_to, t0):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "awaiting_editing")
    return store


def _write_note(tmp_path, content=VALID_NOTE, name="note.md") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _save(store, note_path):
    from interview_pipeline.saving import save_note

    return save_note(store, SID, note_path)


def test_save_note_success_registers_and_advances_state(
    make_store, drive_to, state_name, t0, tmp_path
):
    """正常系: 要約が登録され draft_saved へ進む（ファイルはそのまま）。"""
    store = _setup(make_store, drive_to, t0)
    note = _write_note(tmp_path)

    saved = _save(store, note)

    assert saved == note
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == VALID_NOTE

    session = store.get(SID)
    assert state_name(session.state) == "draft_saved"
    assert session.draft_saved_at is not None


def test_save_note_with_audio_aborts(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """登録対象が音声ファイルなら中止＋例外＋needs_reprocess。"""
    store = _setup(make_store, drive_to, t0)
    audio = tmp_path / "note.wav"
    audio.write_bytes(b"dummy-audio")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio)

    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "awaiting_editing"
    assert session.error_kind == "audio_content_detected"
    assert session.draft_saved_at is None


def test_save_note_with_uppercase_audio_extension_aborts(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """大文字の音声・動画拡張子でも中止になる。"""
    store = _setup(make_store, drive_to, t0)
    audio = tmp_path / "NOTE.MP4"
    audio.write_bytes(b"dummy-video")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio)

    assert state_name(store.get(SID).state) == "needs_reprocess"


@pytest.mark.parametrize("filename", ["note.m4a", "note.mov", "note.mp3", "note.webm"])
def test_save_note_rejects_various_audio_extensions(
    make_store, drive_to, exc, t0, tmp_path, filename
):
    """手順書列挙の拡張子はいずれも登録対象にできない。"""
    store = _setup(make_store, drive_to, t0)
    audio = tmp_path / filename
    audio.write_bytes(b"dummy")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio)


def test_save_note_missing_file_rejected(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """要約ファイルが無ければ NoteContentError で状態は変わらない。"""
    store = _setup(make_store, drive_to, t0)

    with pytest.raises(exc("NoteContentError")):
        _save(store, tmp_path / "missing.md")

    session = store.get(SID)
    assert state_name(session.state) == "awaiting_editing"
    assert session.draft_saved_at is None


def test_save_note_empty_file_rejected(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """空の要約ファイルは NoteContentError で状態は変わらない。"""
    store = _setup(make_store, drive_to, t0)
    note = _write_note(tmp_path, content="  \n")

    with pytest.raises(exc("NoteContentError")):
        _save(store, note)

    session = store.get(SID)
    assert state_name(session.state) == "awaiting_editing"
    assert session.draft_saved_at is None


def test_save_note_twice_is_idempotent(make_store, drive_to, state_name, t0, tmp_path):
    """既に draft_saved でも再登録でき、状態と記録は保たれる。"""
    store = _setup(make_store, drive_to, t0)
    note = _write_note(tmp_path)

    _save(store, note)
    first_saved_at = store.get(SID).draft_saved_at
    saved = _save(store, note)

    session = store.get(SID)
    assert saved == note
    assert state_name(session.state) == "draft_saved"
    assert session.draft_saved_at == first_saved_at
