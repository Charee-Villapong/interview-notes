"""特性テスト（C-124）: VM側コピー削除の状態条件。

根拠: docs/状態管理.md「削除の条件」（正本）と pipeline/SPEC.md の
guards 契約（``vm_copy_deletable`` の項）。

正本では VM側コピーの削除条件は「処理完了（``processing`` を
正常終了している）かつ 要約の生成確認
（``draft_saved`` 以降の状態で、``draft_saved_at`` が記録されている）
の両方」である。本人の決定（C-124）により実装を正本へ合わせるため、
本ファイルは適合後の判定を固定する:

- ``draft_saved`` より前の状態では、フラグ2条件
  （``processing_completed`` と ``draft_saved_at``）を満たしても
  削除不可であり、``execute_vm_deletion`` は拒否する。
- ``needs_reprocess``（脇道の状態）でも削除不可。
- ``draft_saved`` 以降の状態では、2条件を満たせば削除可であり、
  ``execute_vm_deletion`` は従来どおり削除する。
- 状態が文字列で保持されている duck-typing セッションでも同じ
  判定になり、属性が欠けたセッションは安全側に False になる。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

SID = "2026-11-09_01"

#: draft_saved より前の本線の状態（docs/状態管理.md「状態一覧」の順序）。
PRE_DRAFT_STATES = [
    "recorded",
    "transfer_pending",
    "transferring",
    "received",
    "processing",
    "awaiting_editing",
]

#: 「draft_saved 以降」の状態（docs/状態管理.md「削除の条件」）。
POST_DRAFT_STATES = [
    "draft_saved",
    "final_saved",
    "deletable",
    "closed",
]


def _store_with_flags(make_store, drive_to, t0, target):
    """目標状態まで進め、フラグ2条件を満たしたストアを返す。

    ``draft_saved`` より前の状態でも ``processing_completed`` と
    ``draft_saved_at`` を直接満たすことで、状態条件だけが判定を
    分ける状況を作る（既存テストが属性を直接立てる流儀に倣う）。
    """
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, target)
    session = store.get(SID)
    session.processing_completed = True
    session.draft_saved_at = t0
    store.save()
    return store


# ---------------------------------------------------------------------------
# vm_copy_deletable: draft_saved より前はフラグを満たしても False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", PRE_DRAFT_STATES)
def test_vm_copy_deletable_false_before_draft_saved(
    make_store, drive_to, state_name, t0, target
):
    """draft_saved より前では、2条件を満たしても False。"""
    from interview_pipeline.guards import vm_copy_deletable

    store = _store_with_flags(make_store, drive_to, t0, target)
    session = store.get(SID)
    assert state_name(session.state) == target
    assert session.processing_completed is True
    assert session.draft_saved_at is not None
    assert vm_copy_deletable(session) is False


def test_vm_copy_deletable_false_in_needs_reprocess(make_store, drive_to, t0):
    """needs_reprocess では、2条件を満たしても False。"""
    from interview_pipeline.guards import vm_copy_deletable

    store = _store_with_flags(make_store, drive_to, t0, "needs_reprocess")
    session = store.get(SID)
    assert session.processing_completed is True
    assert session.draft_saved_at is not None
    assert vm_copy_deletable(session) is False


# ---------------------------------------------------------------------------
# vm_copy_deletable: draft_saved 以降は2条件を満たせば True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", POST_DRAFT_STATES)
def test_vm_copy_deletable_true_from_draft_saved_onwards(
    make_store, drive_to, state_name, t0, target
):
    """draft_saved 以降では、2条件を満たせば True。"""
    from interview_pipeline.guards import vm_copy_deletable

    store = _store_with_flags(make_store, drive_to, t0, target)
    session = store.get(SID)
    assert state_name(session.state) == target
    assert session.processing_completed is True
    assert session.draft_saved_at is not None
    assert vm_copy_deletable(session) is True


# ---------------------------------------------------------------------------
# execute_vm_deletion: draft_saved より前と needs_reprocess は拒否
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", PRE_DRAFT_STATES)
def test_execute_vm_deletion_refused_before_draft_saved(
    make_store, drive_to, exc, filekit, t0, tmp_path, target
):
    """draft_saved より前では削除が拒否され、ファイルと記録が残る。"""
    from interview_pipeline.guards import execute_vm_deletion

    store = _store_with_flags(make_store, drive_to, t0, target)
    inbox_root = tmp_path / "inbox"
    kept = filekit.write(inbox_root / SID / "system_track.m4a", b"dummy")

    with pytest.raises(exc("DeletionGuardError")):
        execute_vm_deletion(store, SID, inbox_root)

    assert kept.exists()
    assert store.get(SID).vm_copy_deleted_at is None


def test_execute_vm_deletion_refused_in_needs_reprocess(
    make_store, drive_to, exc, filekit, t0, tmp_path
):
    """needs_reprocess では削除が拒否され、ファイルと記録が残る。"""
    from interview_pipeline.guards import execute_vm_deletion

    store = _store_with_flags(make_store, drive_to, t0, "needs_reprocess")
    inbox_root = tmp_path / "inbox"
    kept = filekit.write(inbox_root / SID / "system_track.m4a", b"dummy")

    with pytest.raises(exc("DeletionGuardError")):
        execute_vm_deletion(store, SID, inbox_root)

    assert kept.exists()
    assert store.get(SID).vm_copy_deleted_at is None


# ---------------------------------------------------------------------------
# execute_vm_deletion: draft_saved 以降は従来どおり削除する
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", POST_DRAFT_STATES)
def test_execute_vm_deletion_deletes_from_draft_saved_onwards(
    make_store, drive_to, filekit, t0, tmp_path, target
):
    """draft_saved 以降ではフォルダが消え、削除時刻が記録される。"""
    from interview_pipeline.guards import execute_vm_deletion

    store = _store_with_flags(make_store, drive_to, t0, target)
    inbox_root = tmp_path / "inbox"
    target_file = filekit.write(inbox_root / SID / "system_track.m4a", b"dummy")

    returned = execute_vm_deletion(store, SID, inbox_root)

    assert returned == inbox_root / SID
    assert not target_file.exists()
    assert store.get(SID).vm_copy_deleted_at is not None


# ---------------------------------------------------------------------------
# duck-typing セッション（文字列状態・属性欠落）での判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", PRE_DRAFT_STATES)
def test_vm_deletable_with_string_state_before_draft_saved(t0, state):
    """文字列状態でも draft_saved より前は False。"""
    from interview_pipeline.guards import vm_copy_deletable

    session = SimpleNamespace(state=state, processing_completed=True, draft_saved_at=t0)
    assert vm_copy_deletable(session) is False


def test_vm_deletable_with_string_state_needs_reprocess(t0):
    """文字列状態の needs_reprocess でも False。"""
    from interview_pipeline.guards import vm_copy_deletable

    session = SimpleNamespace(
        state="needs_reprocess", processing_completed=True, draft_saved_at=t0
    )
    assert vm_copy_deletable(session) is False


@pytest.mark.parametrize("state", POST_DRAFT_STATES)
def test_vm_deletable_with_string_state_from_draft_saved_onwards(t0, state):
    """文字列状態でも draft_saved 以降は True（"draft_saved" を含む）。"""
    from interview_pipeline.guards import vm_copy_deletable

    session = SimpleNamespace(state=state, processing_completed=True, draft_saved_at=t0)
    assert vm_copy_deletable(session) is True


def test_vm_deletable_with_missing_attributes_is_false(t0):
    """属性が欠けたセッションは安全側に False。

    状態だけが欠けていても（フラグ2条件があっても）True にはならず、
    フラグが欠けていても draft_saved 以降の状態だけでは True にならない。
    """
    from interview_pipeline.guards import vm_copy_deletable

    assert vm_copy_deletable(SimpleNamespace()) is False
    assert (
        vm_copy_deletable(SimpleNamespace(processing_completed=True, draft_saved_at=t0))
        is False
    )
    assert (
        vm_copy_deletable(SimpleNamespace(state="draft_saved", draft_saved_at=t0))
        is False
    )
    assert (
        vm_copy_deletable(
            SimpleNamespace(state="draft_saved", processing_completed=True)
        )
        is False
    )
