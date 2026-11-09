"""特性テスト（C-125）: Mac側コピー削除の状態条件。

根拠: docs/状態管理.md「削除の条件」（正本）と pipeline/SPEC.md の
guards 契約（``mac_deletion_eligible`` の項）。

正本では Mac側コピーの削除可否は「確定版が保存済み
（``final_saved`` 以降）かつ未処理でないこと
（``needs_reprocess`` でないこと）」であり、期限（最長7日）は
削除の許可条件ではない。本人の決定（C-125）により実装を正本へ
合わせるため、本ファイルは適合後の判定を固定する:

- 判定は状態そのもので行う。``final_saved_at`` の記録の有無は
  条件ではない。つまり ``final_saved`` 以降の状態なら
  ``final_saved_at`` が無くても True、``final_saved`` 未到達の
  状態なら ``final_saved_at`` が記録されていても False。
- ``needs_reprocess`` は ``final_saved_at`` の有無によらず False。
- 経過日数（録音からの日数）は条件ではない。古いセッションでも
  判定は状態だけで決まる。
- 状態が文字列で保持されている duck-typing セッションでも同じ
  判定になり、状態の属性が欠けたセッションは安全側に False。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

SID = "2026-11-09_01"

#: 「final_saved 以降」の状態（docs/状態管理.md「削除の条件」）。
POST_FINAL_STATES = [
    "final_saved",
    "deletable",
    "closed",
]

#: final_saved より前の本線の状態（docs/状態管理.md「状態一覧」の順序）。
PRE_FINAL_STATES = [
    "recorded",
    "transfer_pending",
    "transferring",
    "received",
    "processing",
    "awaiting_editing",
    "draft_saved",
]


def _store_at_state(make_store, drive_to, t0, target, created_at=None):
    """目標状態まで正規の遷移で進めたストアを返す。"""
    store = make_store()
    store.register(SID, created_at or t0)
    drive_to(store, SID, target)
    return store


def _set_final_saved_at(store, value):
    """セッションの final_saved_at を直接書き換えて保存する。

    既存テストが属性を直接立てる流儀に倣い、状態とタイムスタンプの
    記録を独立に組み合わせるために使う。
    """
    session = store.get(SID)
    session.final_saved_at = value
    store.save()
    return store.get(SID)


# ---------------------------------------------------------------------------
# mac_deletion_eligible: final_saved 以降は final_saved_at によらず True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", POST_FINAL_STATES)
def test_mac_eligible_from_final_saved_onwards_without_timestamp(
    make_store, drive_to, state_name, t0, target
):
    """final_saved 以降では、final_saved_at が None でも True。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, target)
    session = _set_final_saved_at(store, None)
    assert state_name(session.state) == target
    assert session.final_saved_at is None
    assert mac_deletion_eligible(session) is True


@pytest.mark.parametrize("target", POST_FINAL_STATES)
def test_mac_eligible_from_final_saved_onwards_with_timestamp(
    make_store, drive_to, state_name, t0, target
):
    """final_saved 以降では、final_saved_at があっても True。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, target)
    session = store.get(SID)
    assert state_name(session.state) == target
    assert session.final_saved_at is not None
    assert mac_deletion_eligible(session) is True


# ---------------------------------------------------------------------------
# mac_deletion_eligible: final_saved より前は final_saved_at によらず False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", PRE_FINAL_STATES)
def test_mac_not_eligible_before_final_saved_even_with_timestamp(
    make_store, drive_to, state_name, t0, target
):
    """final_saved 未到達では、final_saved_at が記録されていても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, target)
    session = _set_final_saved_at(store, t0)
    assert state_name(session.state) == target
    assert session.final_saved_at is not None
    assert mac_deletion_eligible(session) is False


@pytest.mark.parametrize("target", PRE_FINAL_STATES)
def test_mac_not_eligible_before_final_saved_without_timestamp(
    make_store, drive_to, state_name, t0, target
):
    """final_saved 未到達では、final_saved_at が無くても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, target)
    session = _set_final_saved_at(store, None)
    assert state_name(session.state) == target
    assert session.final_saved_at is None
    assert mac_deletion_eligible(session) is False


# ---------------------------------------------------------------------------
# mac_deletion_eligible: needs_reprocess は final_saved_at によらず False
# ---------------------------------------------------------------------------


def test_mac_not_eligible_in_needs_reprocess_without_timestamp(
    make_store, drive_to, t0
):
    """needs_reprocess では、final_saved_at が無くても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, "needs_reprocess")
    session = store.get(SID)
    assert session.final_saved_at is None
    assert mac_deletion_eligible(session) is False


def test_mac_not_eligible_in_needs_reprocess_with_timestamp(
    make_store, drive_to, state_of, t0
):
    """final_saved を経由しても needs_reprocess 中は False。

    final_saved_at の記録が残っている状態を、test_guards.py の
    Mac 節と同じ作り方（final_saved へ進めてから遷移）で作る。
    """
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, "final_saved")
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="reprocess_requested",
        resume_state=state_of("transfer_pending"),
    )
    session = store.get(SID)
    assert session.final_saved_at is not None
    assert mac_deletion_eligible(session) is False


# ---------------------------------------------------------------------------
# mac_deletion_eligible: 経過日数は条件ではない
# ---------------------------------------------------------------------------


def test_mac_eligible_for_old_session_by_state_without_timestamp(
    make_store, drive_to, t0
):
    """古くても final_saved 以降なら、final_saved_at が無くても True。

    既存テスト（test_guards.py）は final_saved_at ありの古い
    セッションの True を固定しているため、ここではタイムスタンプ
    無しの組み合わせで経過日数が条件でないことを固定する。
    """
    from interview_pipeline.guards import mac_deletion_eligible

    old = t0 - timedelta(days=30)
    store = _store_at_state(make_store, drive_to, t0, "final_saved", created_at=old)
    session = _set_final_saved_at(store, None)
    assert session.final_saved_at is None
    assert mac_deletion_eligible(session) is True


def test_mac_not_eligible_for_old_session_before_final_saved_with_timestamp(
    make_store, drive_to, t0
):
    """古くても final_saved 未到達なら、final_saved_at があっても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    old = t0 - timedelta(days=30)
    store = _store_at_state(make_store, drive_to, t0, "draft_saved", created_at=old)
    session = _set_final_saved_at(store, t0)
    assert session.final_saved_at is not None
    assert mac_deletion_eligible(session) is False


# ---------------------------------------------------------------------------
# duck-typing セッション（文字列状態・属性欠落）での判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", POST_FINAL_STATES)
def test_mac_eligible_with_string_state_without_timestamp_attribute(state):
    """文字列状態でも final_saved 以降は final_saved_at 無しで True。

    final_saved_at の属性自体を持たないセッションでも、状態が
    final_saved 以降なら True になる。
    """
    from interview_pipeline.guards import mac_deletion_eligible

    assert mac_deletion_eligible(SimpleNamespace(state=state)) is True


@pytest.mark.parametrize("state", POST_FINAL_STATES)
def test_mac_eligible_with_string_state_and_none_timestamp(state):
    """文字列状態＋final_saved_at=None でも final_saved 以降は True。"""
    from interview_pipeline.guards import mac_deletion_eligible

    session = SimpleNamespace(state=state, final_saved_at=None)
    assert mac_deletion_eligible(session) is True


@pytest.mark.parametrize("state", POST_FINAL_STATES)
def test_mac_eligible_with_string_state_and_timestamp(t0, state):
    """文字列状態＋final_saved_at ありでも final_saved 以降は True。"""
    from interview_pipeline.guards import mac_deletion_eligible

    session = SimpleNamespace(state=state, final_saved_at=t0)
    assert mac_deletion_eligible(session) is True


@pytest.mark.parametrize("state", PRE_FINAL_STATES)
def test_mac_not_eligible_with_string_state_despite_timestamp(t0, state):
    """文字列状態でも final_saved 未到達は final_saved_at があっても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    session = SimpleNamespace(state=state, final_saved_at=t0)
    assert mac_deletion_eligible(session) is False


@pytest.mark.parametrize("state", PRE_FINAL_STATES)
def test_mac_not_eligible_with_string_state_without_timestamp(state):
    """文字列状態でも final_saved 未到達は final_saved_at 無しでも False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    assert mac_deletion_eligible(SimpleNamespace(state=state)) is False
    session = SimpleNamespace(state=state, final_saved_at=None)
    assert mac_deletion_eligible(session) is False


def test_mac_not_eligible_with_string_state_needs_reprocess(t0):
    """文字列状態の needs_reprocess は final_saved_at の有無によらず False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    assert mac_deletion_eligible(SimpleNamespace(state="needs_reprocess")) is False
    session = SimpleNamespace(state="needs_reprocess", final_saved_at=t0)
    assert mac_deletion_eligible(session) is False
    session = SimpleNamespace(state="needs_reprocess", final_saved_at=None)
    assert mac_deletion_eligible(session) is False


def test_mac_eligible_with_missing_state_attribute_is_false(t0):
    """状態の属性が欠けたセッションは安全側に False。

    final_saved_at だけがあっても、状態が無ければ True にはならない
    （test_characterization_guards_files.py の既存固定と同じ方向）。
    """
    from interview_pipeline.guards import mac_deletion_eligible

    assert mac_deletion_eligible(SimpleNamespace()) is False
    assert mac_deletion_eligible(SimpleNamespace(final_saved_at=t0)) is False
    assert mac_deletion_eligible(SimpleNamespace(final_saved_at=None)) is False
