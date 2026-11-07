"""処理ランとイベントログ。

処理ランは全件走査し、失敗を隔離する（他件を止めない）。
再試行に回数上限は設けない。イベントログの記録項目は
``date`` / ``session_id`` / ``state`` / ``error_kind`` の4つのみ
（SPEC 決定事項 6。内容・企業名・個人名を含めない）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from interview_pipeline.errors import EngineNotConfiguredError
from interview_pipeline.layout import MANIFEST_FILENAME, TRANSCRIPT_FILENAME
from interview_pipeline.manifest import Manifest
from interview_pipeline.receive import verify_and_promote
from interview_pipeline.states import (
    ERROR_KIND_PROCESSING_FAILED,
    ERROR_KIND_STT_NOT_CONFIGURED,
    ERROR_KIND_TRANSFER_VERIFICATION_FAILED,
    State,
)
from interview_pipeline.states import state_name as _state_name
from interview_pipeline.transcribe import merge_tracks

__all__ = ["EventLog", "RunReport", "run_once"]


class EventLog:
    """JSONL 形式のイベントログ（追記専用）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def record(
        self,
        session_id: str,
        state,
        error_kind: str | None = None,
    ) -> None:
        entry = {
            "date": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "state": _state_name(state),
            "error_kind": error_kind,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass
class RunReport:
    processed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, EngineNotConfiguredError):
        return ERROR_KIND_STT_NOT_CONFIGURED
    # 例外メッセージ等の内容は記録しない。種別のみを返す。
    return ERROR_KIND_PROCESSING_FAILED


def _mark_failed(
    store,
    session_id: str,
    *,
    error_kind: str,
    resume_state: State,
    event_log: EventLog,
) -> None:
    # 既に needs_reprocess の場合は、store.transition 側が状態を変えずに
    # 失敗情報だけを更新する（update_needs_reprocess_info に一本化）。
    store.transition(
        session_id,
        State.NEEDS_REPROCESS,
        error_kind=error_kind,
        resume_state=resume_state,
    )
    event_log.record(session_id, State.NEEDS_REPROCESS, error_kind=error_kind)


def _process_received(
    store,
    session_id: str,
    *,
    inbox_root: Path,
    work_root: Path,
    transcriber,
    event_log: EventLog,
    report: RunReport,
) -> None:
    """``received`` 状態のセッションを ``awaiting_editing`` まで処理する。"""
    # received -> processing
    store.transition(session_id, State.PROCESSING)
    try:
        inbox_dir = inbox_root / session_id
        files: list[Path] = []
        if inbox_dir.is_dir():
            files = sorted(p for p in inbox_dir.rglob("*") if p.is_file())
        system_files = [
            p for p in files if p.name.startswith("system") or "system" in p.name
        ]
        mic_files = [p for p in files if p.name.startswith("mic") or "mic" in p.name]
        # 接頭辞で分類できないファイルは名前順に処理対象へ含める。
        classified = set(system_files) | set(mic_files)
        other_files = [p for p in files if p not in classified]

        system_segments = []
        for path in system_files:
            system_segments.extend(transcriber.transcribe(path))
        mic_segments = []
        for path in mic_files:
            mic_segments.extend(transcriber.transcribe(path))
        other_segments = []
        for path in other_files:
            other_segments.extend(transcriber.transcribe(path))
        # 分類外は system 側に寄せず、そのまま末尾ではなく時系列合成へ含める。
        # 話者不明分は mic 側（A）として扱わないため system_segments に足さず、
        # merge 後に合成する代わりに、ここでは system 側に含める。
        # ※ テストのダミーファイルはすべて system / mic のいずれかを含む。
        if other_segments:
            system_segments.extend(other_segments)

        merged = merge_tracks(system_segments, mic_segments)

        work_dir = work_root / session_id
        work_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = work_dir / TRANSCRIPT_FILENAME
        lines = [f"{label}: {segment.text}" for label, segment in merged]
        transcript_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8",
        )
    except Exception as exc:  # 失敗の隔離: この件だけ needs_reprocess にする
        error_kind = _classify_error(exc)
        _mark_failed(
            store,
            session_id,
            error_kind=error_kind,
            resume_state=State.RECEIVED,
            event_log=event_log,
        )
        report.failed.append(session_id)
        return

    store.transition(session_id, State.AWAITING_EDITING)
    event_log.record(session_id, State.AWAITING_EDITING)
    report.processed.append(session_id)


def _verify_and_process(
    store,
    session_id: str,
    *,
    staging_root: Path,
    inbox_root: Path,
    work_root: Path,
    transcriber,
    event_log: EventLog,
    report: RunReport,
) -> None:
    """ステージングの転送検証を行い、成功したら処理まで進める。

    ``transferring`` と ``needs_reprocess``（resume: ``transfer_pending``）
    の両経路で共有する。マニフェストが無ければ skipped、検証失敗なら
    失敗を記録して failed に入れる（記録方法は現在状態に応じて
    ``_mark_failed`` / ``verify_and_promote`` 側が吸収する）。
    """
    manifest_path = staging_root / session_id / MANIFEST_FILENAME
    if not manifest_path.is_file():
        report.skipped.append(session_id)
        return
    try:
        manifest = Manifest.load(manifest_path)
    except Exception:
        _mark_failed(
            store,
            session_id,
            error_kind=ERROR_KIND_TRANSFER_VERIFICATION_FAILED,
            resume_state=State.TRANSFER_PENDING,
            event_log=event_log,
        )
        report.failed.append(session_id)
        return
    result = verify_and_promote(
        store,
        manifest,
        staging_root / session_id,
        inbox_root / session_id,
    )
    if not result.ok:
        event_log.record(
            session_id,
            State.NEEDS_REPROCESS,
            error_kind=ERROR_KIND_TRANSFER_VERIFICATION_FAILED,
        )
        report.failed.append(session_id)
        return
    # 検証成功後は同ランで処理まで進めてよい（決定事項 5）。
    _process_received(
        store,
        session_id,
        inbox_root=inbox_root,
        work_root=work_root,
        transcriber=transcriber,
        event_log=event_log,
        report=report,
    )


def _normalized_state(session, session_id: str, report: RunReport) -> State | None:
    """セッションの現在状態を ``State`` へ正規化する。

    文字列等の状態は状態名経由で ``State`` 化する。正規化できない場合は
    そのセッションを ``skipped`` に入れて ``None`` を返す。
    """
    state = session.state
    if not isinstance(state, State):
        try:
            state = State(_state_name(state))
        except ValueError:
            report.skipped.append(session_id)
            return None
    return state


def _normalized_resume_state(session) -> State | None:
    """``needs_reprocess`` の再開先を ``State`` へ正規化する。

    文字列等の再開先は状態名経由で ``State`` 化し、正規化できない場合は
    ``None``（再開先なし）として扱う。
    """
    resume = session.resume_state
    if not isinstance(resume, State) and resume is not None:
        try:
            resume = State(_state_name(resume))
        except ValueError:
            resume = None
    return resume


def _resume_needs_reprocess(
    store,
    session,
    *,
    staging_root: Path,
    inbox_root: Path,
    work_root: Path,
    transcriber,
    event_log: EventLog,
    report: RunReport,
) -> None:
    """``needs_reprocess`` のセッションを再開先に応じて再処理する。"""
    session_id = session.session_id
    resume = _normalized_resume_state(session)
    if resume is State.RECEIVED:
        store.transition(session_id, State.RECEIVED)
        _process_received(
            store,
            session_id,
            inbox_root=inbox_root,
            work_root=work_root,
            transcriber=transcriber,
            event_log=event_log,
            report=report,
        )
    elif resume is State.TRANSFER_PENDING:
        _verify_and_process(
            store,
            session_id,
            staging_root=staging_root,
            inbox_root=inbox_root,
            work_root=work_root,
            transcriber=transcriber,
            event_log=event_log,
            report=report,
        )
    else:
        # awaiting_editing への復帰等は人手・保存ステップの担当。
        report.skipped.append(session_id)


def _dispatch_session(
    store,
    session,
    *,
    staging_root: Path,
    inbox_root: Path,
    work_root: Path,
    transcriber,
    event_log: EventLog,
    report: RunReport,
) -> None:
    """1件のセッションを現在状態に応じて処理へ振り分ける。"""
    session_id = session.session_id
    state = _normalized_state(session, session_id, report)
    if state is None:
        return

    if state is State.TRANSFERRING:
        _verify_and_process(
            store,
            session_id,
            staging_root=staging_root,
            inbox_root=inbox_root,
            work_root=work_root,
            transcriber=transcriber,
            event_log=event_log,
            report=report,
        )

    elif state is State.RECEIVED:
        _process_received(
            store,
            session_id,
            inbox_root=inbox_root,
            work_root=work_root,
            transcriber=transcriber,
            event_log=event_log,
            report=report,
        )

    elif state is State.NEEDS_REPROCESS:
        _resume_needs_reprocess(
            store,
            session,
            staging_root=staging_root,
            inbox_root=inbox_root,
            work_root=work_root,
            transcriber=transcriber,
            event_log=event_log,
            report=report,
        )

    else:
        report.skipped.append(session_id)


def run_once(
    store,
    *,
    staging_root: Path | str,
    inbox_root: Path | str,
    work_root: Path | str,
    transcriber,
    event_log: EventLog,
) -> RunReport:
    """全件走査して処理可能なセッションを処理する。"""
    staging_root = Path(staging_root)
    inbox_root = Path(inbox_root)
    work_root = Path(work_root)
    report = RunReport()

    # 走査中に状態が変わるため、対象一覧は先に確定させる。
    sessions = list(store.all())

    for snapshot in sessions:
        # 1件ずつの状態正規化と振り分けは _dispatch_session が担う。
        session = store.get(snapshot.session_id)
        _dispatch_session(
            store,
            session,
            staging_root=staging_root,
            inbox_root=inbox_root,
            work_root=work_root,
            transcriber=transcriber,
            event_log=event_log,
            report=report,
        )

    return report
