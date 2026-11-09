"""保存・削除ガードのテスト。

根拠: pipeline/SPEC.md の ``guards.py`` 契約、docs/状態管理.md
「削除の条件」、docs/手順書.md「保存ガード」「削除ガード」。

前提（解釈）: ``execute_vm_deletion`` の ``inbox_dir`` は inbox ルートで、
削除対象はその下の ``<session_id>/`` フォルダ（SPEC の文言
「inbox のセッションフォルダを削除し」からの判断）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

SID = "2026-11-09_01"

#: 手順書「保存ガード」に列挙された音声・動画拡張子。
EXPECTED_AUDIO_VIDEO_EXTENSIONS = {
    ".m4a",
    ".mov",
    ".mkv",
    ".wav",
    ".mp3",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".aiff",
    ".caf",
    ".mp4",
    ".webm",
}


def _session(make_store, t0, target="recorded", created_at=None):
    store = make_store()
    store.register(SID, created_at or t0)
    return store, store.get(SID)


# ---------------------------------------------------------------------------
# 音声・動画拡張子の検出
# ---------------------------------------------------------------------------


def test_audio_video_extensions_cover_procedure_list():
    """AUDIO_VIDEO_EXTENSIONS は手順書の拡張子一覧を含む。"""
    from interview_pipeline.guards import AUDIO_VIDEO_EXTENSIONS

    normalized = {str(ext).lower() for ext in AUDIO_VIDEO_EXTENSIONS}
    assert EXPECTED_AUDIO_VIDEO_EXTENSIONS <= normalized


@pytest.mark.parametrize(
    "filename",
    [
        "take.m4a",
        "take.wav",
        "take.mp3",
        "take.mp4",
        "take.mov",
        "take.webm",
        "take.mkv",
        "take.aac",
        "take.flac",
        "take.ogg",
        "take.opus",
        "take.aiff",
        "take.caf",
    ],
)
def test_find_audio_files_detects_each_extension(tmp_path, filename):
    """各音声・動画拡張子のファイルが検出される。"""
    from interview_pipeline.guards import find_audio_files

    target = tmp_path / filename
    target.write_bytes(b"dummy")
    assert find_audio_files([target]) == [target]


@pytest.mark.parametrize("filename", ["TAKE.M4A", "Take.WAV", "take.MP4", "take.Mp3"])
def test_find_audio_files_is_case_insensitive(tmp_path, filename):
    """拡張子の判定は大文字小文字を区別しない。"""
    from interview_pipeline.guards import find_audio_files

    target = tmp_path / filename
    target.write_bytes(b"dummy")
    assert find_audio_files([target]) == [target]


def test_find_audio_files_ignores_non_audio(tmp_path):
    """md / txt / json 等は音声として検出されない。"""
    from interview_pipeline.guards import find_audio_files

    paths = []
    for name in ["note.md", "memo.txt", "data.json", "manifest"]:
        path = tmp_path / name
        path.write_bytes(b"dummy")
        paths.append(path)
    assert find_audio_files(paths) == []


def test_find_audio_files_mixed_list(tmp_path):
    """混在リストからは音声・動画だけが返る。"""
    from interview_pipeline.guards import find_audio_files

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"dummy")
    text = tmp_path / "b.md"
    text.write_bytes(b"dummy")
    result = find_audio_files([text, audio])
    assert result == [audio]


def test_assert_no_audio_files_raises_on_audio(tmp_path, exc):
    """音声が1件でもあれば AudioContentError で保存中止になる。"""
    from interview_pipeline.guards import assert_no_audio_files

    target = tmp_path / "take.m4a"
    target.write_bytes(b"dummy")
    with pytest.raises(exc("AudioContentError")):
        assert_no_audio_files([target])


def test_assert_no_audio_files_raises_on_uppercase_extension(tmp_path, exc):
    """大文字拡張子の音声でも中止になる。"""
    from interview_pipeline.guards import assert_no_audio_files

    target = tmp_path / "TAKE.WAV"
    target.write_bytes(b"dummy")
    with pytest.raises(exc("AudioContentError")):
        assert_no_audio_files([target])


def test_assert_no_audio_files_passes_without_audio(tmp_path):
    """音声がなければ例外なし。"""
    from interview_pipeline.guards import assert_no_audio_files

    target = tmp_path / "note.md"
    target.write_bytes(b"dummy")
    assert_no_audio_files([target])
    assert_no_audio_files([])


# ---------------------------------------------------------------------------
# VM側コピーの削除ガード
# ---------------------------------------------------------------------------


def test_vm_copy_deletable_only_when_completed_and_draft_saved(
    make_store, drive_to, t0
):
    """処理完了＋下書き保存確認の両方を満たす時だけ True。"""
    from interview_pipeline.guards import vm_copy_deletable

    store, session = _session(make_store, t0)
    drive_to(store, SID, "draft_saved")
    session = store.get(SID)
    session.processing_completed = True
    store.save()
    assert session.draft_saved_at is not None
    assert vm_copy_deletable(session) is True


def test_vm_copy_not_deletable_without_processing_completed(make_store, t0):
    """下書き保存済みでも処理完了でなければ False。"""
    from interview_pipeline.guards import vm_copy_deletable

    _, session = _session(make_store, t0)
    session.processing_completed = False
    # draft_saved_at だけ記録済みの状態を直接作る。
    session.draft_saved_at = t0
    assert vm_copy_deletable(session) is False


def test_vm_copy_not_deletable_without_draft_saved(make_store, t0):
    """処理完了でも下書き保存確認がなければ False。"""
    from interview_pipeline.guards import vm_copy_deletable

    _, session = _session(make_store, t0)
    session.processing_completed = True
    assert session.draft_saved_at is None
    assert vm_copy_deletable(session) is False


def test_vm_copy_not_deletable_when_neither_condition(make_store, t0):
    """両方欠けば False。"""
    from interview_pipeline.guards import vm_copy_deletable

    _, session = _session(make_store, t0)
    assert vm_copy_deletable(session) is False


def test_execute_vm_deletion_removes_folder_and_records_timestamp(
    make_store, drive_to, filekit, t0, tmp_path
):
    """削除可のときだけフォルダが消え、vm_copy_deleted_at が記録される。"""
    from interview_pipeline.guards import execute_vm_deletion

    store, _ = _session(make_store, t0)
    drive_to(store, SID, "draft_saved")
    session = store.get(SID)
    session.processing_completed = True
    store.save()

    inbox_root = tmp_path / "inbox"
    target_dir = inbox_root / SID
    filekit.write(target_dir / "system_track.m4a", b"dummy-system")
    filekit.write(target_dir / "mic_track.m4a", b"dummy-mic")

    execute_vm_deletion(store, SID, inbox_root)

    assert not target_dir.exists()
    assert store.get(SID).vm_copy_deleted_at is not None


def test_execute_vm_deletion_refused_without_draft_saved(
    make_store, drive_to, exc, filekit, t0, tmp_path
):
    """下書き未保存なら例外で拒否され、ファイルは残る。"""
    from interview_pipeline.guards import execute_vm_deletion

    store, _ = _session(make_store, t0)
    drive_to(store, SID, "received")
    session = store.get(SID)
    session.processing_completed = True  # 片方の条件だけ満たす
    store.save()

    inbox_root = tmp_path / "inbox"
    kept = filekit.write(inbox_root / SID / "system_track.m4a", b"dummy")

    with pytest.raises(exc("DeletionGuardError")):
        execute_vm_deletion(store, SID, inbox_root)

    assert kept.exists(), "拒否されたらファイルは残らなければならない"
    assert store.get(SID).vm_copy_deleted_at is None


def test_execute_vm_deletion_refused_without_processing_completed(
    make_store, filekit, t0, tmp_path, exc
):
    """処理未完了なら例外で拒否され、ファイルは残る。"""
    from interview_pipeline.guards import execute_vm_deletion

    store, session = _session(make_store, t0)
    session.draft_saved_at = t0  # 片方の条件だけ満たす
    session.processing_completed = False
    store.save()

    inbox_root = tmp_path / "inbox"
    kept = filekit.write(inbox_root / SID / "mic_track.m4a", b"dummy")

    with pytest.raises(exc("DeletionGuardError")):
        execute_vm_deletion(store, SID, inbox_root)

    assert kept.exists()
    assert store.get(SID).vm_copy_deleted_at is None


# ---------------------------------------------------------------------------
# Mac側コピーの削除可否
# ---------------------------------------------------------------------------


def test_mac_eligible_when_final_saved(make_store, drive_to, t0):
    """final_saved 到達済み＋未処理でないときだけ True。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store, _ = _session(make_store, t0)
    drive_to(store, SID, "final_saved")
    session = store.get(SID)
    assert session.final_saved_at is not None
    assert mac_deletion_eligible(session) is True


def test_mac_not_eligible_before_final_saved(make_store, drive_to, t0):
    """final_saved 未到達なら False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store, _ = _session(make_store, t0)
    drive_to(store, SID, "draft_saved")
    assert mac_deletion_eligible(store.get(SID)) is False


def test_mac_not_eligible_when_needs_reprocess_even_if_final_saved_before(
    make_store, drive_to, state_of, t0
):
    """final_saved 記録があっても needs_reprocess 中は False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store, _ = _session(make_store, t0)
    drive_to(store, SID, "final_saved")
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="reprocess_requested",
        resume_state=state_of("transfer_pending"),
    )
    session = store.get(SID)
    assert session.final_saved_at is not None  # 記録自体は残っている前提
    assert mac_deletion_eligible(session) is False


def test_mac_not_eligible_for_30_day_old_unprocessed(make_store, t0):
    """録音から30日経った未処理セッションでも False。

    期限は削除の許可条件ではない（状態管理.md「削除の条件」）。
    """
    from interview_pipeline.guards import mac_deletion_eligible

    old = t0 - timedelta(days=30)
    store, _ = _session(make_store, t0, created_at=old)
    # recorded から直接 needs_reprocess へ（任意→needs_reprocess）。
    store.transition(SID, _needs_reprocess_state())
    session = store.get(SID)
    assert mac_deletion_eligible(session) is False


def _needs_reprocess_state():
    """needs_reprocess の State メンバを引く小物。"""
    from interview_pipeline.states import State

    for member in State:
        value = member.value if isinstance(member.value, str) else None
        if value == "needs_reprocess" or member.name.lower() == "needs_reprocess":
            return member
    raise AssertionError("needs_reprocess 状態がありません")


def test_mac_eligible_for_old_final_saved_session(make_store, drive_to, t0):
    """逆に、古くても final_saved 済みなら True（経過日数は条件でない）。"""
    from interview_pipeline.guards import mac_deletion_eligible

    old = t0 - timedelta(days=30)
    store, _ = _session(make_store, t0, created_at=old)
    drive_to(store, SID, "final_saved")
    assert mac_deletion_eligible(store.get(SID)) is True


# ---------------------------------------------------------------------------
# 期限超過の未処理の検出（可視化用）
# ---------------------------------------------------------------------------


def test_find_overdue_unprocessed_detects_old_unfinished(make_store, drive_to, t0):
    """録音から7日超＋final_saved 未到達は「期限超過の未処理」に挙がる。"""
    from interview_pipeline.guards import find_overdue_unprocessed

    now = t0 + timedelta(days=8)
    store, _ = _session(make_store, t0)
    drive_to(store, SID, "received")

    overdue = find_overdue_unprocessed(store, now)
    assert [s.session_id for s in overdue] == [SID]


def test_find_overdue_unprocessed_includes_old_needs_reprocess(make_store, t0):
    """古い needs_reprocess（未処理）も検出対象に含まれる。"""
    from interview_pipeline.guards import find_overdue_unprocessed

    now = t0 + timedelta(days=30)
    store, _ = _session(make_store, t0)
    store.transition(SID, _needs_reprocess_state())

    overdue = find_overdue_unprocessed(store, now, days=7)
    assert [s.session_id for s in overdue] == [SID]


def test_find_overdue_unprocessed_excludes_recent(make_store, t0):
    """7日以内の未処理は挙がらない。"""
    from interview_pipeline.guards import find_overdue_unprocessed

    now = t0 + timedelta(days=6)
    store, _ = _session(make_store, t0)

    assert find_overdue_unprocessed(store, now) == []


def test_find_overdue_unprocessed_excludes_final_saved(make_store, drive_to, t0):
    """古くても final_saved 到達済みは挙がらない。"""
    from interview_pipeline.guards import find_overdue_unprocessed

    now = t0 + timedelta(days=30)
    store, _ = _session(make_store, t0)
    drive_to(store, SID, "final_saved")

    assert find_overdue_unprocessed(store, now) == []


def test_find_overdue_unprocessed_respects_days_argument(make_store, t0):
    """days 引数で閾値を変えられる。"""
    from interview_pipeline.guards import find_overdue_unprocessed

    now = t0 + timedelta(days=3)
    store, _ = _session(make_store, t0)

    assert find_overdue_unprocessed(store, now, days=7) == []
    overdue = find_overdue_unprocessed(store, now, days=2)
    assert [s.session_id for s in overdue] == [SID]


def test_find_overdue_unprocessed_is_visualization_only(make_store, state_name, t0):
    """検出は可視化専用で、セッションの状態や削除記録を変えない。"""
    from interview_pipeline.guards import find_overdue_unprocessed

    now = t0 + timedelta(days=10)
    store, _ = _session(make_store, t0)
    overdue = find_overdue_unprocessed(store, now)

    assert len(overdue) == 1
    assert isinstance(overdue[0], type(store.get(SID)))
    session = store.get(SID)
    assert state_name(session.state) == "recorded"
    assert session.final_saved_at is None
    assert session.vm_copy_deleted_at is None
    assert session.mac_copy_deleted_at is None
