"""セッションストアのテスト。

根拠: pipeline/SPEC.md の ``store.py`` 契約と docs/状態管理.md
（二重処理・二重保存の防止、needs_reprocess の resume_state / error_kind、
draft_saved / final_saved 時の自動記録）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

SID = "2026-11-09_01"
SID2 = "2026-11-09_02"


def test_register_creates_session_in_recorded_state(make_store, state_name, t0):
    """登録直後のセッションは recorded で、主要フィールドは初期値。"""
    store = make_store()
    session = store.register(SID, t0)

    assert session.session_id == SID
    assert state_name(session.state) == "recorded"
    assert session.created_at == t0
    assert session.resume_state is None
    assert session.error_kind is None
    assert session.draft_saved_at is None
    assert session.final_saved_at is None
    assert session.vm_copy_deleted_at is None
    assert session.mac_copy_deleted_at is None
    assert session.processing_completed is False


def test_register_is_idempotent(make_store, t0):
    """同一セッションIDの登録は冪等で、二重に作られない。"""
    store = make_store()
    first = store.register(SID, t0)
    # 2回目に別の created_at を渡しても既存レコードが返る。
    later = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)
    second = store.register(SID, later)

    assert len(store.all()) == 1
    assert second.session_id == first.session_id
    assert second.created_at == first.created_at


def test_get_and_all(make_store, t0):
    """get で取得でき、all は登録した全件を返す。"""
    store = make_store()
    store.register(SID, t0)
    store.register(SID2, t0)

    assert store.get(SID).session_id == SID
    assert store.get(SID2).session_id == SID2
    assert {s.session_id for s in store.all()} == {SID, SID2}


def test_get_unknown_session_raises(make_store):
    """未登録のセッションIDの取得は例外になる。"""
    store = make_store()
    with pytest.raises(Exception):
        store.get("2026-11-09_99")


def test_transition_updates_state(make_store, drive_to, state_of, state_name, t0):
    """正規の遷移で状態が更新される。"""
    store = make_store()
    store.register(SID, t0)
    store.transition(SID, state_of("transfer_pending"))
    assert state_name(store.get(SID).state) == "transfer_pending"
    store.transition(SID, state_of("transferring"))
    assert state_name(store.get(SID).state) == "transferring"


def test_invalid_transition_rejected_and_state_unchanged(
    make_store, exc, state_of, state_name, t0
):
    """不正な遷移は例外で拒否され、状態は変わらない。"""
    store = make_store()
    store.register(SID, t0)

    with pytest.raises(exc("InvalidTransitionError")):
        store.transition(SID, state_of("received"))  # recorded から直接は不可
    assert state_name(store.get(SID).state) == "recorded"


def test_transition_to_draft_saved_records_timestamp(
    make_store, drive_to, state_name, t0
):
    """draft_saved へ進むとき draft_saved_at が自動で記録される。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "awaiting_editing")
    assert store.get(SID).draft_saved_at is None

    drive_to(store, SID, "draft_saved")
    session = store.get(SID)
    assert state_name(session.state) == "draft_saved"
    assert session.draft_saved_at is not None


def test_transition_to_final_saved_records_timestamp(
    make_store, drive_to, state_name, t0
):
    """final_saved へ進むとき final_saved_at が自動で記録される。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "awaiting_review")
    assert store.get(SID).final_saved_at is None

    drive_to(store, SID, "final_saved")
    session = store.get(SID)
    assert state_name(session.state) == "final_saved"
    assert session.final_saved_at is not None


def test_transition_to_needs_reprocess_records_error_and_resume(
    make_store, drive_to, state_of, state_name, t0
):
    """needs_reprocess への遷移で error_kind と resume_state が保持される。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")

    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="stt_not_configured",
        resume_state=state_of("received"),
    )
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert session.error_kind == "stt_not_configured"
    assert state_name(session.resume_state) == "received"


def test_state_persists_across_store_reload(make_store, drive_to, state_name, t0):
    """状態は永続化され、ストアを再読み込みしても再開できる。"""
    store = make_store("persist.json")
    store.register(SID, t0)
    drive_to(store, SID, "draft_saved")
    store.save()

    reloaded = make_store("persist.json")
    sessions = reloaded.all()
    if not sessions:
        # コンストラクタで読み込まない実装の場合は明示的に load する。
        reloaded.load()
        sessions = reloaded.all()

    assert len(sessions) == 1
    session = reloaded.get(SID)
    assert state_name(session.state) == "draft_saved"
    assert session.draft_saved_at is not None
    assert session.created_at == t0


def test_needs_reprocess_fields_persist_across_reload(
    make_store, drive_to, state_of, state_name, t0
):
    """needs_reprocess の error_kind / resume_state も再読み込みで残る。"""
    store = make_store("persist2.json")
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="transfer_verification_failed",
        resume_state=state_of("transfer_pending"),
    )
    store.save()

    reloaded = make_store("persist2.json")
    if not reloaded.all():
        reloaded.load()
    session = reloaded.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert session.error_kind == "transfer_verification_failed"
    assert state_name(session.resume_state) == "transfer_pending"


def test_sessions_with_distinct_timestamps_kept_separate(make_store, t0):
    """別セッションはそれぞれの created_at を保持する。"""
    store = make_store()
    other_time = t0 + timedelta(days=1)
    store.register(SID, t0)
    store.register(SID2, other_time)

    assert store.get(SID).created_at == t0
    assert store.get(SID2).created_at == other_time
