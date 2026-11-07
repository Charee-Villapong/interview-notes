"""状態と遷移の定義。

正本は ``docs/状態管理.md`` の「状態一覧」「遷移条件」。
SPEC 決定事項 1・2:
- ``closed`` は終端状態とし、外向きの遷移を持たない。
- ``needs_reprocess`` からの復帰は ``resume_state`` への遷移のみとし、
  ``TRANSITIONS`` では ``needs_reprocess -> {transfer_pending, received,
  awaiting_editing}`` を許可する。
"""

from __future__ import annotations

from enum import Enum

from interview_pipeline.errors import InvalidTransitionError

__all__ = [
    "ERROR_KIND_AUDIO_CONTENT_DETECTED",
    "ERROR_KIND_PROCESSING_FAILED",
    "ERROR_KIND_STT_NOT_CONFIGURED",
    "ERROR_KIND_TRANSFER_VERIFICATION_FAILED",
    "ERROR_KIND_UNCLASSIFIED_TRACK_FILE",
    "InvalidTransitionError",
    "State",
    "TRANSITIONS",
    "assert_transition",
    "can_transition",
    "coerce_state",
    "state_name",
]

# error_kind の契約値（SPEC の公開インターフェースで固定された文字列）。
# セッションとイベントログの両方に同じ値が記録されるため、リテラルを
# 散在させずここに集約する。値そのものは契約なので変更しない。
ERROR_KIND_TRANSFER_VERIFICATION_FAILED = "transfer_verification_failed"
ERROR_KIND_STT_NOT_CONFIGURED = "stt_not_configured"
ERROR_KIND_PROCESSING_FAILED = "processing_failed"
ERROR_KIND_AUDIO_CONTENT_DETECTED = "audio_content_detected"
ERROR_KIND_UNCLASSIFIED_TRACK_FILE = "unclassified_track_file"


class State(str, Enum):
    RECORDED = "recorded"
    TRANSFER_PENDING = "transfer_pending"
    TRANSFERRING = "transferring"
    RECEIVED = "received"
    PROCESSING = "processing"
    AWAITING_EDITING = "awaiting_editing"
    DRAFT_SAVED = "draft_saved"
    AWAITING_REVIEW = "awaiting_review"
    FINAL_SAVED = "final_saved"
    DELETABLE = "deletable"
    CLOSED = "closed"
    NEEDS_REPROCESS = "needs_reprocess"


TRANSITIONS: dict[State, set[State]] = {
    State.RECORDED: {State.TRANSFER_PENDING, State.NEEDS_REPROCESS},
    State.TRANSFER_PENDING: {State.TRANSFERRING, State.NEEDS_REPROCESS},
    State.TRANSFERRING: {State.RECEIVED, State.NEEDS_REPROCESS},
    State.RECEIVED: {State.PROCESSING, State.NEEDS_REPROCESS},
    State.PROCESSING: {State.AWAITING_EDITING, State.NEEDS_REPROCESS},
    State.AWAITING_EDITING: {State.DRAFT_SAVED, State.NEEDS_REPROCESS},
    State.DRAFT_SAVED: {State.AWAITING_REVIEW, State.NEEDS_REPROCESS},
    State.AWAITING_REVIEW: {State.FINAL_SAVED, State.NEEDS_REPROCESS},
    State.FINAL_SAVED: {State.DELETABLE, State.NEEDS_REPROCESS},
    State.DELETABLE: {State.CLOSED, State.NEEDS_REPROCESS},
    State.CLOSED: set(),
    State.NEEDS_REPROCESS: {
        State.TRANSFER_PENDING,
        State.RECEIVED,
        State.AWAITING_EDITING,
    },
}


def coerce_state(state: State | str) -> State:
    """状態を ``State`` へ寄せる（文字列の状態名を吸収する）。

    ``State`` そのものはそのまま返し、それ以外は ``State(str(state))``
    で変換する。未知の名前では ``ValueError`` になる。状態の強制変換は
    この規則に一本化する（ただし ``receive`` のセッション状態の読み取りは、
    ``.value`` を持つduck-typing的な値を吸収する範囲が異なるため、
    そちらの判定方法は現行のまま別に置く）。
    """
    if isinstance(state, State):
        return state
    return State(str(state))


def _coerce(state: State | str) -> State:
    return coerce_state(state)


def state_name(state) -> str:
    """状態値から状態名の文字列を取り出す（寛容な読み取り）。

    ``None`` は空文字列、文字列はそのまま、``.value`` が文字列なら
    その値、それ以外は ``.name`` 等からの文字列化で返す。duck-typing
    的なセッション／モックでも落ちない読み取り専用の規則であり、
    遷移可否などの判定そのものには使わない（判定は ``coerce_state``
    側の規則で行う）。
    """
    if state is None:
        return ""
    if isinstance(state, str):
        return state
    value = getattr(state, "value", None)
    if isinstance(value, str):
        return value
    return str(getattr(state, "name", state)).lower()


def can_transition(src: State | str, dst: State | str) -> bool:
    """``src`` から ``dst`` への遷移が許可されているかを返す。"""
    try:
        src_state = _coerce(src)
        dst_state = _coerce(dst)
    except ValueError:
        return False
    return dst_state in TRANSITIONS.get(src_state, set())


def assert_transition(src: State | str, dst: State | str) -> None:
    """許可されていない遷移なら ``InvalidTransitionError`` を送出する。"""
    if not can_transition(src, dst):
        raise InvalidTransitionError(f"transition not allowed: {src!r} -> {dst!r}")
