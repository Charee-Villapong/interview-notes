"""転送受信と検証（verify_and_promote）。

SPEC 決定事項 3: ``staging_dir`` / ``inbox_dir`` はセッション単位の
ディレクトリ。全ファイルのサイズ・SHA-256 が一致した場合のみ
inbox へ移動し、状態を ``received`` に進める。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from interview_pipeline.manifest import Manifest
from interview_pipeline.states import ERROR_KIND_TRANSFER_VERIFICATION_FAILED, State
from interview_pipeline.store import update_needs_reprocess_info

__all__ = ["ReceiveResult", "verify_and_promote"]


@dataclass
class ReceiveResult:
    ok: bool
    reason: str | None = None


# received 以降とみなす状態（冪等に ok=True を返す）。
_ALREADY_RECEIVED_OR_LATER = {
    State.RECEIVED,
    State.PROCESSING,
    State.AWAITING_EDITING,
    State.DRAFT_SAVED,
    State.AWAITING_REVIEW,
    State.FINAL_SAVED,
    State.DELETABLE,
    State.CLOSED,
}


def _state_of(session) -> State:
    # states.coerce_state とは意図的に別系統として残す: こちらは
    # ``.value`` を持つ duck-typing 的な状態値も ``State`` 化して吸収する
    # （coerce_state は ``str(state)`` 経由のため受理範囲が異なる）。
    # 統一すると受信判定の受理範囲が変わるため、現行の判定方法を保つ。
    state = session.state
    if isinstance(state, State):
        return state
    return State(str(getattr(state, "value", state)))


# ハッシュ計算でファイルを読み進める単位（バイト数）。性能だけに効く
# 調整値で、検証結果には影響しない。現行値をそのまま名前付ける。
_SHA256_CHUNK_SIZE = 1024 * 1024


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_SHA256_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(store, session, reason: str) -> ReceiveResult:
    if _state_of(session) is State.NEEDS_REPROCESS:
        # 既に needs_reprocess の場合は状態を保ったまま失敗情報を更新する。
        update_needs_reprocess_info(
            session,
            error_kind=ERROR_KIND_TRANSFER_VERIFICATION_FAILED,
            resume_state=State.TRANSFER_PENDING,
        )
        store.save()
    else:
        store.transition(
            session.session_id,
            State.NEEDS_REPROCESS,
            error_kind=ERROR_KIND_TRANSFER_VERIFICATION_FAILED,
            resume_state=State.TRANSFER_PENDING,
        )
    return ReceiveResult(ok=False, reason=reason)


def verify_and_promote(
    store,
    manifest: Manifest,
    staging_dir: Path | str,
    inbox_dir: Path | str,
) -> ReceiveResult:
    staging = Path(staging_dir)
    inbox = Path(inbox_dir)
    session = store.get(manifest.session_id)
    current = _state_of(session)

    if current in _ALREADY_RECEIVED_OR_LATER:
        return ReceiveResult(ok=True)

    # 全ファイルの検証（1件でも不一致・欠落があれば昇格しない）。
    for entry in manifest.files:
        candidate = staging / entry.name
        if not candidate.is_file():
            return _fail(store, session, f"missing file: {entry.name}")
        actual_size = candidate.stat().st_size
        if actual_size != entry.size:
            return _fail(
                store,
                session,
                f"size mismatch: {entry.name}",
            )
        actual_hash = _sha256_of(candidate)
        if actual_hash != entry.sha256:
            return _fail(
                store,
                session,
                f"sha256 mismatch: {entry.name}",
            )

    # 全件一致した場合のみ inbox へ移動する。
    inbox.mkdir(parents=True, exist_ok=True)
    for entry in manifest.files:
        source = staging / entry.name
        destination = inbox / entry.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.move(str(source), str(destination))
        elif not destination.is_file():
            return _fail(store, session, f"missing file: {entry.name}")

    # 状態を received へ進める（現在状態に応じた正規経路を使う）。
    current = _state_of(store.get(manifest.session_id))
    if current is State.TRANSFERRING:
        store.transition(manifest.session_id, State.RECEIVED)
    elif current is State.NEEDS_REPROCESS:
        store.transition(manifest.session_id, State.RECEIVED)
    elif current is State.TRANSFER_PENDING:
        store.transition(manifest.session_id, State.TRANSFERRING)
        store.transition(manifest.session_id, State.RECEIVED)
    elif current is State.RECORDED:
        store.transition(manifest.session_id, State.TRANSFER_PENDING)
        store.transition(manifest.session_id, State.TRANSFERRING)
        store.transition(manifest.session_id, State.RECEIVED)
    elif current not in _ALREADY_RECEIVED_OR_LATER:
        store.transition(manifest.session_id, State.RECEIVED)

    return ReceiveResult(ok=True)
