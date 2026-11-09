"""特性テスト: Mac側コピー削除の状態条件（mac_deletion_eligible）。

根拠: docs/状態管理.md「削除の条件」（正本）と pipeline/SPEC.md の
guards 契約（``mac_deletion_eligible`` の項）。

本人の仕様決定（2026-11-09、正式フロー化）により、Mac側コピーの
削除可否は「要約が非公開リポジトリへ push 済み」に読み替えられた:

- 判定は「``final_saved`` 以降の状態」かつ「``note_published_at``
  が記録されていること」の両方。どちらか欠ければ False。
- ``needs_reprocess`` は記録の有無によらず False。
- 経過日数（録音からの日数）は条件ではない。
- 状態が文字列で保持されている duck-typing セッションでも同じ
  判定になり、属性が欠けたセッションは安全側に False。
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


def _set_published_at(store, value):
    """セッションの note_published_at を直接書き換えて保存する。"""
    session = store.get(SID)
    session.note_published_at = value
    store.save()
    return store.get(SID)


# ---------------------------------------------------------------------------
# mac_deletion_eligible: push 済み（状態＋記録）のときだけ True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", POST_FINAL_STATES)
def test_mac_eligible_from_final_saved_onwards_with_publish_record(
    make_store, drive_to, state_name, t0, target
):
    """final_saved 以降＋push 記録ありなら True。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, target)
    session = _set_published_at(store, t0)
    assert state_name(session.state) == target
    assert session.note_published_at is not None
    assert mac_deletion_eligible(session) is True


@pytest.mark.parametrize("target", POST_FINAL_STATES)
def test_mac_not_eligible_from_final_saved_onwards_without_publish_record(
    make_store, drive_to, state_name, t0, target
):
    """final_saved 以降でも push 記録が無ければ False（push 前は消さない）。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, target)
    session = _set_published_at(store, None)
    assert state_name(session.state) == target
    assert session.note_published_at is None
    assert mac_deletion_eligible(session) is False


@pytest.mark.parametrize("target", PRE_FINAL_STATES)
def test_mac_not_eligible_before_final_saved_even_with_publish_record(
    make_store, drive_to, state_name, t0, target
):
    """final_saved 未到達では、push 記録があっても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, target)
    session = _set_published_at(store, t0)
    assert state_name(session.state) == target
    assert session.note_published_at is not None
    assert mac_deletion_eligible(session) is False


@pytest.mark.parametrize("target", PRE_FINAL_STATES)
def test_mac_not_eligible_before_final_saved_without_publish_record(
    make_store, drive_to, state_name, t0, target
):
    """final_saved 未到達では、push 記録が無くても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, target)
    session = _set_published_at(store, None)
    assert state_name(session.state) == target
    assert session.note_published_at is None
    assert mac_deletion_eligible(session) is False


# ---------------------------------------------------------------------------
# mac_deletion_eligible: needs_reprocess は記録の有無によらず False
# ---------------------------------------------------------------------------


def test_mac_not_eligible_in_needs_reprocess(make_store, drive_to, t0):
    """needs_reprocess では、push 記録が無くても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, "needs_reprocess")
    session = store.get(SID)
    assert session.note_published_at is None
    assert mac_deletion_eligible(session) is False


def test_mac_not_eligible_in_needs_reprocess_with_publish_record(
    make_store, drive_to, state_of, t0
):
    """final_saved を経由しても needs_reprocess 中は False。

    push 記録が残っている状態を、final_saved へ進めてから遷移させて作る。
    """
    from interview_pipeline.guards import mac_deletion_eligible

    store = _store_at_state(make_store, drive_to, t0, "final_saved")
    _set_published_at(store, t0)
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="reprocess_requested",
        resume_state=state_of("transfer_pending"),
    )
    session = store.get(SID)
    assert session.note_published_at is not None
    assert mac_deletion_eligible(session) is False


# ---------------------------------------------------------------------------
# mac_deletion_eligible: 経過日数は条件ではない
# ---------------------------------------------------------------------------


def test_mac_eligible_for_old_session_with_publish_record(make_store, drive_to, t0):
    """古くても、final_saved 以降＋push 記録ありなら True。"""
    from interview_pipeline.guards import mac_deletion_eligible

    old = t0 - timedelta(days=30)
    store = _store_at_state(make_store, drive_to, t0, "final_saved", created_at=old)
    session = _set_published_at(store, t0)
    assert session.note_published_at is not None
    assert mac_deletion_eligible(session) is True


def test_mac_not_eligible_for_old_session_before_final_saved(make_store, drive_to, t0):
    """古くても final_saved 未到達なら、push 記録があっても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    old = t0 - timedelta(days=30)
    store = _store_at_state(make_store, drive_to, t0, "draft_saved", created_at=old)
    session = _set_published_at(store, t0)
    assert session.note_published_at is not None
    assert mac_deletion_eligible(session) is False


# ---------------------------------------------------------------------------
# duck-typing セッション（文字列状態・属性欠落）での判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", POST_FINAL_STATES)
def test_mac_eligible_with_string_state_and_publish_record(t0, state):
    """文字列状態でも、final_saved 以降＋push 記録ありは True。"""
    from interview_pipeline.guards import mac_deletion_eligible

    session = SimpleNamespace(state=state, note_published_at=t0)
    assert mac_deletion_eligible(session) is True


@pytest.mark.parametrize("state", POST_FINAL_STATES)
def test_mac_not_eligible_with_string_state_without_publish_record(state):
    """文字列状態でも、push 記録が無ければ final_saved 以降でも False。

    note_published_at の属性自体を持たないセッションも安全側に False。
    """
    from interview_pipeline.guards import mac_deletion_eligible

    assert mac_deletion_eligible(SimpleNamespace(state=state)) is False
    session = SimpleNamespace(state=state, note_published_at=None)
    assert mac_deletion_eligible(session) is False


@pytest.mark.parametrize("state", PRE_FINAL_STATES)
def test_mac_not_eligible_with_string_state_despite_publish_record(t0, state):
    """文字列状態でも final_saved 未到達は push 記録があっても False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    session = SimpleNamespace(state=state, note_published_at=t0)
    assert mac_deletion_eligible(session) is False


def test_mac_not_eligible_with_string_state_needs_reprocess(t0):
    """文字列状態の needs_reprocess は push 記録の有無によらず False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    assert mac_deletion_eligible(SimpleNamespace(state="needs_reprocess")) is False
    session = SimpleNamespace(state="needs_reprocess", note_published_at=t0)
    assert mac_deletion_eligible(session) is False
    session = SimpleNamespace(state="needs_reprocess", note_published_at=None)
    assert mac_deletion_eligible(session) is False


def test_mac_eligible_with_missing_state_attribute_is_false(t0):
    """状態の属性が欠けたセッションは安全側に False。"""
    from interview_pipeline.guards import mac_deletion_eligible

    assert mac_deletion_eligible(SimpleNamespace()) is False
    assert mac_deletion_eligible(SimpleNamespace(note_published_at=t0)) is False
    assert mac_deletion_eligible(SimpleNamespace(note_published_at=None)) is False
