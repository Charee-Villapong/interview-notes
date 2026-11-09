"""特性テスト（現状固定）: C-058 guards のファイル単体削除分岐ほか。

根拠: refactoring-candidates-2026-11-07.md の interview-notes
「テストが薄い箇所」（guards.py のファイル単体削除分岐は未検証で、
既存テストはフォルダ削除のみ）、pipeline/SPEC.md の guards.py
契約、docs/状態管理.md「削除の条件」。

（C-124: 本人の決定により実装を正本へ適合させた）
文書の正本では VM側コピーの削除条件は「処理完了かつ
draft_saved 以降の状態で draft_saved_at が記録されている」
の二重条件である。本人の決定により実装を正本へ適合させた
（C-124）ため、本ファイルの該当テストは適合後の挙動
（draft_saved 以降の状態でなければ削除不可）を固定する。

本ファイルが固定するのは:
- execute_vm_deletion のファイル単体削除・対象不在時の成功・
  戻り値（削除対象パス）
- 適合後の VM 削除条件（recorded では削除不可・削除拒否）
- duck-typing セッション（文字列状態・属性欠落モック）での
  mac_deletion_eligible / vm_copy_deletable の現行挙動
- find_audio_files の文字列パス入力・拡張子なし・ドットファイル名
- find_overdue_unprocessed の created_at 欠落セッションの扱い
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

SID = "2026-11-09_01"


def _deletable_store(make_store, drive_to, t0):
    # 正本の削除条件（処理完了＋draft_saved 以降＋draft_saved_at）を満たす。
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "draft_saved")
    session = store.get(SID)
    session.processing_completed = True
    session.draft_saved_at = t0
    store.save()
    return store


# ---------------------------------------------------------------------------
# execute_vm_deletion のファイル単体・対象不在の分岐
# ---------------------------------------------------------------------------


def test_execute_vm_deletion_removes_single_file_target(
    make_store, drive_to, filekit, t0, tmp_path
):
    # 削除対象がフォルダでなく単一ファイルでも削除され、時刻が残る。
    from interview_pipeline.guards import execute_vm_deletion

    store = _deletable_store(make_store, drive_to, t0)
    inbox_root = tmp_path / "inbox"
    inbox_root.mkdir()
    target = inbox_root / SID
    target.write_bytes(b"dummy-single-file")

    returned = execute_vm_deletion(store, SID, inbox_root)

    assert returned == target
    assert not target.exists()
    assert store.get(SID).vm_copy_deleted_at is not None


def test_execute_vm_deletion_succeeds_when_target_missing(
    make_store, drive_to, t0, tmp_path
):
    # 対象フォルダが存在しなくても成功し、削除時刻だけが記録される。
    from interview_pipeline.guards import execute_vm_deletion

    store = _deletable_store(make_store, drive_to, t0)
    inbox_root = tmp_path / "inbox"  # 作らない

    returned = execute_vm_deletion(store, SID, inbox_root)

    assert returned == inbox_root / SID
    assert store.get(SID).vm_copy_deleted_at is not None


def test_execute_vm_deletion_returns_session_path(make_store, drive_to, t0, tmp_path):
    # 戻り値は inbox ルート直下のセッションパスである。
    from interview_pipeline.guards import execute_vm_deletion

    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "draft_saved")
    inbox_root = tmp_path / "inbox"
    (inbox_root / SID).mkdir(parents=True)

    returned = execute_vm_deletion(store, SID, inbox_root)

    assert returned == inbox_root / SID


# ---------------------------------------------------------------------------
# VM削除の状態条件（C-124適合後）: draft_saved 未満では削除不可
# ---------------------------------------------------------------------------


def test_vm_deletable_false_in_recorded_state_after_c124_alignment(make_store, t0):
    # 適合後の正本どおりの挙動: 状態が recorded では False になる。
    # docs/状態管理.md の正本は「draft_saved 以降の状態」も条件に含む
    # ため、recorded のままフラグ2条件を直接満たしても False を返す。
    from interview_pipeline.guards import vm_copy_deletable

    store = make_store()
    store.register(SID, t0)
    session = store.get(SID)
    session.processing_completed = True
    session.draft_saved_at = t0
    store.save()
    assert session.state.value == "recorded"  # draft_saved 以降ではない
    assert vm_copy_deletable(session) is False


def test_execute_vm_deletion_refused_in_recorded_state_after_c124_alignment(
    make_store, exc, filekit, t0, tmp_path
):
    # 適合後の正本どおりの挙動: recorded 状態では実削除が拒否される。
    # vm_copy_deletable の状態条件の帰結として、execute_vm_deletion も
    # DeletionGuardError で拒否し、ファイルは消えず削除時刻も残らない。
    from interview_pipeline.guards import execute_vm_deletion

    store = make_store()
    store.register(SID, t0)
    session = store.get(SID)
    session.processing_completed = True
    session.draft_saved_at = t0
    store.save()
    inbox_root = tmp_path / "inbox"
    target_file = filekit.write(inbox_root / SID / "system_track.m4a", b"dummy")

    with pytest.raises(exc("DeletionGuardError")):
        execute_vm_deletion(store, SID, inbox_root)

    assert target_file.exists()
    assert store.get(SID).vm_copy_deleted_at is None


# ---------------------------------------------------------------------------
# duck-typing セッションでの判定（文字列状態・属性欠落）
# ---------------------------------------------------------------------------


def test_mac_eligible_with_plain_string_states(t0):
    # 状態が State でなく文字列でも mac_deletion_eligible は判定できる。
    from interview_pipeline.guards import mac_deletion_eligible

    assert (
        mac_deletion_eligible(
            SimpleNamespace(final_saved_at=t0, state="needs_reprocess")
        )
        is False
    )
    assert (
        mac_deletion_eligible(SimpleNamespace(final_saved_at=t0, state="final_saved"))
        is True
    )
    assert (
        mac_deletion_eligible(SimpleNamespace(final_saved_at=t0, state="deletable"))
        is True
    )


def test_mac_eligible_with_missing_attributes_is_false():
    # 属性を持たないモック的セッションは安全側に倒れて False。
    from interview_pipeline.guards import mac_deletion_eligible

    assert mac_deletion_eligible(SimpleNamespace()) is False
    assert mac_deletion_eligible(SimpleNamespace(final_saved_at=None)) is False


def test_vm_deletable_with_missing_attributes_is_false():
    # 属性を持たないモック的セッションは vm_copy_deletable も False。
    from interview_pipeline.guards import vm_copy_deletable

    assert vm_copy_deletable(SimpleNamespace()) is False
    assert vm_copy_deletable(SimpleNamespace(processing_completed=True)) is False


# ---------------------------------------------------------------------------
# find_audio_files の入力形と拡張子の端
# ---------------------------------------------------------------------------


def test_find_audio_files_accepts_string_paths(tmp_path):
    # 文字列パスを渡しても Path として返り、大文字拡張子も検出する。
    from interview_pipeline.guards import find_audio_files

    audio = tmp_path / "TAKE.MP3"
    audio.write_bytes(b"dummy")
    text = tmp_path / "note.md"
    text.write_bytes(b"dummy")

    assert find_audio_files([str(text), str(audio)]) == [audio]


def test_find_audio_files_ignores_extensionless_and_dotfile_names(tmp_path):
    # 現行挙動: 拡張子の無い名前と「.m4a」という名前自体は検出されない。
    # Path(".m4a").suffix が空になるため、ドットファイル名は
    # 音声拡張子に一致しない。
    from interview_pipeline.guards import find_audio_files

    plain = tmp_path / "manifest"
    plain.write_bytes(b"dummy")
    dotfile = tmp_path / ".m4a"
    dotfile.write_bytes(b"dummy")

    assert find_audio_files([plain, dotfile]) == []


# ---------------------------------------------------------------------------
# find_overdue_unprocessed の端
# ---------------------------------------------------------------------------


def test_find_overdue_unprocessed_skips_session_without_created_at(make_store, t0):
    # created_at が欠けたセッションは期限超過の判定対象外になる。
    from datetime import timedelta

    from interview_pipeline.guards import find_overdue_unprocessed

    store = make_store()
    store.register(SID, t0)
    store.get(SID).created_at = None

    assert find_overdue_unprocessed(store, t0 + timedelta(days=30)) == []
