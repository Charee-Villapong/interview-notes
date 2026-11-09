"""状態と遷移のテスト。

根拠: docs/状態管理.md「状態一覧」「遷移条件」（正本）と
pipeline/SPEC.md の ``states.py`` 契約
（``State`` / ``TRANSITIONS`` / ``can_transition`` / ``assert_transition``）。
"""

from __future__ import annotations

import pytest

#: 状態管理.md「状態一覧」の全状態。
EXPECTED_STATES = {
    "recorded",
    "transfer_pending",
    "transferring",
    "received",
    "processing",
    "awaiting_editing",
    "draft_saved",
    "final_saved",
    "deletable",
    "closed",
    "needs_reprocess",
}

#: 状態管理.md「遷移条件」の表にある遷移（「任意 → needs_reprocess」を除く）。
ALLOWED_TRANSITIONS = [
    ("recorded", "transfer_pending"),
    ("transfer_pending", "transferring"),
    ("transferring", "received"),
    ("transferring", "needs_reprocess"),
    ("received", "processing"),
    ("processing", "awaiting_editing"),
    ("processing", "needs_reprocess"),
    ("awaiting_editing", "draft_saved"),
    ("awaiting_editing", "needs_reprocess"),
    ("draft_saved", "final_saved"),
    ("draft_saved", "needs_reprocess"),
    ("final_saved", "deletable"),
    ("deletable", "closed"),
]

#: 表にない遷移（不正として拒否されるべきもの）。
INVALID_TRANSITIONS = [
    ("recorded", "transferring"),
    ("recorded", "received"),
    ("recorded", "processing"),
    ("recorded", "awaiting_editing"),
    ("recorded", "draft_saved"),
    ("recorded", "final_saved"),
    ("recorded", "deletable"),
    ("recorded", "closed"),
    ("recorded", "recorded"),
    ("transfer_pending", "recorded"),
    ("transfer_pending", "received"),
    ("transfer_pending", "processing"),
    ("transfer_pending", "draft_saved"),
    ("transfer_pending", "closed"),
    ("transferring", "transfer_pending"),
    ("transferring", "processing"),
    ("transferring", "awaiting_editing"),
    ("transferring", "draft_saved"),
    ("received", "transfer_pending"),
    ("received", "received"),
    ("received", "awaiting_editing"),
    ("received", "draft_saved"),
    ("received", "final_saved"),
    ("received", "closed"),
    ("processing", "received"),
    ("processing", "draft_saved"),
    ("processing", "final_saved"),
    ("awaiting_editing", "processing"),
    ("awaiting_editing", "final_saved"),
    ("draft_saved", "awaiting_editing"),
    ("draft_saved", "deletable"),
    ("final_saved", "closed"),
    ("deletable", "final_saved"),
    ("closed", "recorded"),
    ("closed", "transfer_pending"),
    ("closed", "deletable"),
]


def test_state_enum_matches_state_table(state_of, state_name):
    """State は状態一覧の11状態ちょうどを持つ。"""
    from interview_pipeline.states import State

    actual = {state_name(member) for member in State}
    assert actual == EXPECTED_STATES
    # state_of で全状態が引けることも確認する。
    for name in EXPECTED_STATES:
        assert state_name(state_of(name)) == name


def test_transitions_table_structure(state_of, state_name):
    """TRANSITIONS は State をキーとし、許可先の集合を持つ。"""
    from interview_pipeline.states import TRANSITIONS, State

    assert isinstance(TRANSITIONS, dict)
    for src, destinations in TRANSITIONS.items():
        assert isinstance(src, State)
        for dst in destinations:
            assert isinstance(dst, State)
    # 表の許可遷移はすべて TRANSITIONS に現れる。
    for src_name, dst_name in ALLOWED_TRANSITIONS:
        assert state_of(dst_name) in TRANSITIONS.get(
            state_of(src_name), set()
        ), f"TRANSITIONS に {src_name} -> {dst_name} がありません"


@pytest.mark.parametrize("src_name,dst_name", ALLOWED_TRANSITIONS)
def test_allowed_transitions(state_of, src_name, dst_name):
    """表にある遷移は許可される（can_transition が True、例外なし）。"""
    from interview_pipeline.states import assert_transition, can_transition

    src, dst = state_of(src_name), state_of(dst_name)
    assert can_transition(src, dst) is True
    assert_transition(src, dst)  # 例外を送出しないこと


@pytest.mark.parametrize("src_name,dst_name", INVALID_TRANSITIONS)
def test_invalid_transitions_rejected(state_of, exc, src_name, dst_name):
    """表にない遷移は拒否される（can_transition が False、例外を送出）。"""
    from interview_pipeline.states import assert_transition, can_transition

    invalid_error = exc("InvalidTransitionError")
    src, dst = state_of(src_name), state_of(dst_name)
    assert can_transition(src, dst) is False
    with pytest.raises(invalid_error):
        assert_transition(src, dst)


@pytest.mark.parametrize(
    "src_name",
    [
        "recorded",
        "transfer_pending",
        "transferring",
        "received",
        "processing",
        "awaiting_editing",
        "draft_saved",
        "final_saved",
        "deletable",
    ],
)
def test_any_state_can_go_to_needs_reprocess(state_of, src_name):
    """「任意 → needs_reprocess」は許可される（失敗全般の受け皿）。

    注: 終端状態 closed からも許可されるかは正本の「任意」の解釈が
    曖昧なため、ここでは対象に含めない（報告で明示）。
    """
    from interview_pipeline.states import assert_transition, can_transition

    src = state_of(src_name)
    dst = state_of("needs_reprocess")
    assert can_transition(src, dst) is True
    assert_transition(src, dst)
