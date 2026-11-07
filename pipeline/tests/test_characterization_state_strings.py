"""特性テスト（現状固定）: C-052 状態の文字列化ヘルパーの現挙動。

根拠: refactoring-candidates-2026-11-07.md の interview-notes 候補3
（状態の文字列化ヘルパーが3系統＋インラインにあり、実装の
フォールバック範囲が微妙に不一致。文字列状態・モック的セッションでの
現挙動を固定するテストが薄い）、pipeline/SPEC.md の ``states.py`` /
``store.py`` 契約。

本ファイルが固定するのは:
- ``can_transition`` / ``assert_transition`` の文字列入力
  （正常な状態名は State と等価、未知の名前は False / 例外）
- ``SessionStore.transition`` の文字列 dst / resume_state の受け付けと、
  needs_reprocess からの復帰時の失敗情報クリア
- 既に needs_reprocess のセッションへの再 transition（状態を保ったまま
  error_kind / resume_state だけが更新される。receive / runner /
  saving の各フォールバックが依拠する現行挙動）
- ``EventLog.record`` の文字列状態と State の出力同等性
"""

from __future__ import annotations

import json

import pytest

SID = "2026-11-09_01"


# ---------------------------------------------------------------------------
# can_transition / assert_transition の文字列入力
# ---------------------------------------------------------------------------


def test_can_transition_accepts_plain_strings():
    """状態名の文字列でも can_transition は State と同じ判定をする。"""
    from interview_pipeline.states import can_transition

    assert can_transition("recorded", "transfer_pending") is True
    assert can_transition("recorded", "received") is False
    assert can_transition("closed", "needs_reprocess") is False


def test_can_transition_with_unknown_names_is_false():
    """未知の状態名は例外でなく False になる（現行の _coerce 吸収）。"""
    from interview_pipeline.states import can_transition

    assert can_transition("bogus", "received") is False
    assert can_transition("recorded", "bogus") is False
    assert can_transition("bogus", "bogus") is False


def test_assert_transition_accepts_plain_strings(exc):
    """文字列の許可遷移は通り、不正遷移は InvalidTransitionError。"""
    from interview_pipeline.states import assert_transition

    assert_transition("recorded", "transfer_pending")
    with pytest.raises(exc("InvalidTransitionError")):
        assert_transition("recorded", "received")
    with pytest.raises(exc("InvalidTransitionError")):
        assert_transition("bogus", "received")


# ---------------------------------------------------------------------------
# SessionStore.transition の文字列入力と失敗情報の扱い
# ---------------------------------------------------------------------------


def test_store_transition_accepts_string_dst_and_resume(make_store, state_name, t0):
    """transition の dst / resume_state は文字列でも受け付けられる。"""
    store = make_store()
    store.register(SID, t0)

    store.transition(SID, "transfer_pending")
    assert state_name(store.get(SID).state) == "transfer_pending"

    store.transition(
        SID,
        "needs_reprocess",
        error_kind="dummy_error",
        resume_state="transfer_pending",
    )
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert session.error_kind == "dummy_error"
    assert state_name(session.resume_state) == "transfer_pending"


def test_store_transition_from_needs_reprocess_clears_failure_info(
    make_store, drive_to, state_of, state_name, t0
):
    """needs_reprocess からの復帰で error_kind / resume_state が消える。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="processing_failed",
        resume_state=state_of("received"),
    )

    store.transition(SID, state_of("received"))

    session = store.get(SID)
    assert state_name(session.state) == "received"
    assert session.error_kind is None
    assert session.resume_state is None


def test_store_retransition_in_needs_reprocess_updates_only_failure_info(
    make_store, drive_to, state_of, state_name, t0
):
    """既に needs_reprocess への再 transition は状態を保ち情報だけ更新する。

    遷移表に needs_reprocess → needs_reprocess は無いが、現行実装は
    この自己遷移を例外にせず、error_kind / resume_state の更新として
    扱う（receive 等のフォールバックが依拠する挙動）。
    """
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="processing_failed",
        resume_state=state_of("received"),
    )

    # 例外なく受け付けられること自体が現行挙動。
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="transfer_verification_failed",
        resume_state=state_of("transfer_pending"),
    )

    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert session.error_kind == "transfer_verification_failed"
    assert state_name(session.resume_state) == "transfer_pending"


def test_store_retransition_in_needs_reprocess_keeps_unspecified_fields(
    make_store, drive_to, state_of, state_name, t0
):
    """再 transition で渡さなかった項目は元の値が保持される。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="processing_failed",
        resume_state=state_of("received"),
    )

    store.transition(SID, state_of("needs_reprocess"))  # 引数なし

    session = store.get(SID)
    assert session.error_kind == "processing_failed"
    assert state_name(session.resume_state) == "received"


# ---------------------------------------------------------------------------
# EventLog.record の文字列状態
# ---------------------------------------------------------------------------


def test_event_log_record_string_state_matches_state_output(
    make_event_log, state_of, tmp_path
):
    """文字列で渡した状態も State で渡した場合と同じ JSON になる。"""
    log_path = tmp_path / "events.jsonl"
    event_log = make_event_log(log_path)
    event_log.record(SID, "received")
    event_log.record(SID, state_of("received"))

    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 2
    for record in records:
        assert set(record.keys()) == {"date", "session_id", "state", "error_kind"}
        assert record["session_id"] == SID
        assert record["state"] == "received"
        assert record["error_kind"] is None
