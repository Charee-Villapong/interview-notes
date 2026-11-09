"""特性テスト: saving（save_note）のフォールバック分岐。

根拠: pipeline/SPEC.md の ``saving.py`` 契約。本ファイルが固定するのは:
- 既に needs_reprocess のセッションへの音声登録（遷移できない場合の
  フォールバック直接更新で error_kind / resume_state が書き換わる）
- チェック順序（音声チェックが内容チェックより先）の現行挙動
- 要約ファイル自体が無い・空の場合（NoteContentError、状態不変）
- 既に draft_saved のセッションへの登録（遷移を飛ばして冪等に成功）

既存テストで固定済みの範囲（本ファイルでは重複させない）:
test_saving.py が正常系の登録・音声中止・欠落／空の拒否を固定済み。
"""

from __future__ import annotations

import pytest

SID = "2026-11-09_01"
VALID_NOTE = "# 面談ノート（ダミー）\nダミー本文です。\n"


def _setup(make_store, drive_to, t0, target="awaiting_editing"):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, target)
    return store


def _save(store, note_path):
    from interview_pipeline.saving import save_note

    return save_note(store, SID, note_path)


# ---------------------------------------------------------------------------
# 例外フォールバック（既に needs_reprocess の場合の直接更新）
# ---------------------------------------------------------------------------


def test_save_note_audio_while_needs_reprocess_updates_failure_info(
    make_store, drive_to, state_of, exc, state_name, t0, tmp_path
):
    """既に needs_reprocess でも音声登録は失敗情報が上書きされ例外になる。

    通常遷移が使えない経路（フォールバック）でも error_kind は
    audio_content_detected、resume_state は awaiting_editing になる。
    """
    store = _setup(make_store, drive_to, t0)
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="processing_failed",
        resume_state=state_of("received"),
    )
    audio = tmp_path / "note.wav"
    audio.write_bytes(b"dummy-audio")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio)

    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert session.error_kind == "audio_content_detected"
    assert state_name(session.resume_state) == "awaiting_editing"


# ---------------------------------------------------------------------------
# チェック順序（音声 → 内容）
# ---------------------------------------------------------------------------


def test_save_note_audio_check_precedes_content_check(
    make_store, drive_to, exc, t0, tmp_path
):
    """音声ファイルは、内容の有無より先に音声側の例外で弾かれる。"""
    store = _setup(make_store, drive_to, t0)
    audio = tmp_path / "note.mp3"
    audio.write_bytes(b"")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio)


# ---------------------------------------------------------------------------
# 要約ファイルの欠落・空（状態不変）
# ---------------------------------------------------------------------------


def test_save_note_empty_file_leaves_state_untouched(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """空ファイルは NoteContentError になり、状態も記録も変わらない。"""
    store = _setup(make_store, drive_to, t0)
    note = tmp_path / "note.md"
    note.write_text("", encoding="utf-8")

    with pytest.raises(exc("NoteContentError")):
        _save(store, note)

    session = store.get(SID)
    assert state_name(session.state) == "awaiting_editing"
    assert session.error_kind is None
    assert session.draft_saved_at is None


def test_save_note_missing_file_leaves_state_untouched(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """存在しないファイルも FileNotFound でなく NoteContentError になる。"""
    store = _setup(make_store, drive_to, t0)

    with pytest.raises(exc("NoteContentError")):
        _save(store, tmp_path / "missing.md")

    assert state_name(store.get(SID).state) == "awaiting_editing"


# ---------------------------------------------------------------------------
# 既に draft_saved のセッションへの登録
# ---------------------------------------------------------------------------


def test_save_note_from_draft_saved_state_skips_duplicate_transition(
    make_store, drive_to, state_name, t0, tmp_path
):
    """既に draft_saved なら、遷移を飛ばしてそのまま成功する。"""
    store = _setup(make_store, drive_to, t0, target="draft_saved")
    note = tmp_path / "note.md"
    note.write_text(VALID_NOTE, encoding="utf-8")

    saved = _save(store, note)

    assert saved == note
    session = store.get(SID)
    assert state_name(session.state) == "draft_saved"
    assert session.draft_saved_at is not None
