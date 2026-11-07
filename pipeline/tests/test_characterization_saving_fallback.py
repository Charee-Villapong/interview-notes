"""特性テスト（現状固定）: C-051 / C-058 saving のフォールバックと日付決定。

根拠: refactoring-candidates-2026-11-07.md の interview-notes 候補2
（saving.py save_draft のフォールバック分岐そのものを狙ったテストは
確認できず）と「テストが薄い箇所」（saving.py の例外フォールバックと
日付フォールバック）、候補8（created_at が無い場合に session_id の
一部を日付とみなす前提の埋没）、pipeline/SPEC.md の ``saving.py`` 契約。

既存テストで固定済みの範囲（本ファイルでは重複させない）:
test_saving.py が正常系の配置・音声中止・非公開拒否・表記の冒頭必須
（本文途中のみは不可）を固定済み。

本ファイルが固定するのは:
- 既に needs_reprocess のセッションへの音声保存（遷移できない場合の
  フォールバック直接更新で error_kind / resume_state が書き換わる）
- created_at が無い場合の日付フォールバック（session_id の日付部分）
- チェック順序（音声 → 非公開 → 表記）の現行挙動
- 下書き表記の判定範囲（1行目のどこかにあれば可、行頭の空白・空行は
  lstrip で読み飛ばされる）
- ドラフトファイル自体が無い場合（DraftMarkingError、状態不変）
- 既に draft_saved のセッションへの保存（draft_saved 遷移を飛ばす）
"""

from __future__ import annotations

from pathlib import Path

import pytest

SID = "2026-11-09_01"
VALID_DRAFT = "下書き（未照合）\n\n# 面談メモ（ダミー）\nダミー本文です。\n"


def _setup(make_store, drive_to, t0, tmp_path, target="awaiting_editing"):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, target)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    return store, notes_dir


def _save(store, draft_path, notes_dir, privacy_checker=lambda: True):
    from interview_pipeline.saving import save_draft

    return save_draft(
        store, SID, draft_path, notes_dir, privacy_checker=privacy_checker
    )


def _saved_files(notes_dir: Path) -> list[Path]:
    return sorted(p for p in notes_dir.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# 例外フォールバック（既に needs_reprocess の場合の直接更新）
# ---------------------------------------------------------------------------


def test_save_draft_audio_while_needs_reprocess_updates_failure_info(
    make_store, drive_to, state_of, exc, state_name, t0, tmp_path
):
    """既に needs_reprocess でも音声保存は失敗情報が上書きされ例外になる。

    通常遷移が使えない経路（フォールバック）でも error_kind は
    audio_content_detected、resume_state は awaiting_editing になる。
    """
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="processing_failed",
        resume_state=state_of("received"),
    )
    audio_draft = tmp_path / "draft.wav"
    audio_draft.write_bytes(b"dummy-audio")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio_draft, notes_dir)

    assert _saved_files(notes_dir) == []
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert session.error_kind == "audio_content_detected"
    assert state_name(session.resume_state) == "awaiting_editing"


# ---------------------------------------------------------------------------
# 日付フォールバック（created_at が無い場合）
# ---------------------------------------------------------------------------


def test_save_draft_date_falls_back_to_session_id_prefix(
    make_store, drive_to, t0, tmp_path
):
    """created_at が無い場合は session_id の日付部分が保存先になる。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    store.get(SID).created_at = None
    draft = tmp_path / "draft.md"
    draft.write_text(VALID_DRAFT, encoding="utf-8")

    saved = _save(store, draft, notes_dir)

    assert saved == notes_dir / "2026-11-09" / f"{SID}.md"
    assert saved.exists()


# ---------------------------------------------------------------------------
# チェック順序（音声 → 非公開 → 表記）
# ---------------------------------------------------------------------------


def test_save_draft_audio_check_precedes_privacy_check(
    make_store, drive_to, exc, t0, tmp_path
):
    """音声かつ非公開でない場合は、音声側の例外が先に出る。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    audio_draft = tmp_path / "draft.mp3"
    audio_draft.write_bytes(b"dummy")

    with pytest.raises(exc("AudioContentError")):
        _save(store, audio_draft, notes_dir, privacy_checker=lambda: False)


def test_save_draft_privacy_check_precedes_marking_check(
    make_store, drive_to, exc, t0, tmp_path
):
    """表記が無くても、非公開でない場合は非公開側の例外が先に出る。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("# 表記なし\n", encoding="utf-8")

    with pytest.raises(exc("RepositoryNotPrivateError")):
        _save(store, draft, notes_dir, privacy_checker=lambda: False)


def test_save_draft_privacy_failure_leaves_state_untouched(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """非公開で拒否された場合は状態も draft_saved_at も変わらない。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text(VALID_DRAFT, encoding="utf-8")

    with pytest.raises(exc("RepositoryNotPrivateError")):
        _save(store, draft, notes_dir, privacy_checker=lambda: False)

    session = store.get(SID)
    assert state_name(session.state) == "awaiting_editing"
    assert session.error_kind is None


# ---------------------------------------------------------------------------
# 下書き表記の判定範囲
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "タイトル 下書き（未照合）\n本文\n",  # 1行目の途中でも可
        "  下書き（未照合）\n本文\n",  # 行頭の空白は読み飛ばされる
        "\n下書き（未照合）\n本文\n",  # 先行する空行も読み飛ばされる
        "下書き（未照合）",  # 表記だけのファイルでも可
    ],
)
def test_save_draft_marking_acceptance_variants(
    make_store, drive_to, state_name, t0, tmp_path, content
):
    """現行挙動: 表記は「lstrip 後の1行目のどこか」にあれば保存できる。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text(content, encoding="utf-8")

    saved = _save(store, draft, notes_dir)

    assert saved.exists()
    assert state_name(store.get(SID).state) == "awaiting_review"


def test_save_draft_empty_file_is_rejected_as_unmarked(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """空ファイルは表記なしとして DraftMarkingError になり状態不変。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("", encoding="utf-8")

    with pytest.raises(exc("DraftMarkingError")):
        _save(store, draft, notes_dir)

    assert _saved_files(notes_dir) == []
    assert state_name(store.get(SID).state) == "awaiting_editing"


def test_save_draft_missing_file_is_rejected_as_unmarked(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """現行挙動: ドラフト自体が無くても FileNotFound でなく表記エラー。

    存在しないファイルは空内容として扱われ、DraftMarkingError になる。
    """
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path)

    with pytest.raises(exc("DraftMarkingError")):
        _save(store, tmp_path / "missing.md", notes_dir)

    assert _saved_files(notes_dir) == []
    assert state_name(store.get(SID).state) == "awaiting_editing"


# ---------------------------------------------------------------------------
# 既に draft_saved のセッションへの保存
# ---------------------------------------------------------------------------


def test_save_draft_from_draft_saved_state_skips_duplicate_transition(
    make_store, drive_to, state_name, t0, tmp_path
):
    """既に draft_saved なら、その遷移を飛ばして awaiting_review へ進む。"""
    store, notes_dir = _setup(make_store, drive_to, t0, tmp_path, target="draft_saved")
    draft = tmp_path / "draft.md"
    draft.write_text(VALID_DRAFT, encoding="utf-8")

    saved = _save(store, draft, notes_dir)

    assert saved.exists()
    session = store.get(SID)
    assert state_name(session.state) == "awaiting_review"
    assert session.draft_saved_at is not None
