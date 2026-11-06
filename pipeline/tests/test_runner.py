"""処理ラン（run_once）とイベントログのテスト。

根拠: pipeline/SPEC.md の ``runner.py`` 契約、docs/状態管理.md
「通知・ログ」（全件走査、失敗しても他件を止めない、再試行上限なし、
記録項目は日付・セッションID・状態・エラー種別のみ）、docs/手順書.md
「処理ラン」「STTエンジン」。

前提（解釈）:
- マニフェストは ``staging_root/<session_id>/manifest.json`` に置かれる
  （手順書のデータ領域構成と Manifest.load の存在からの判断）。
- 作業用テキストは ``work_root/<session_id>/transcript.md``。
"""

from __future__ import annotations

import json
from pathlib import Path

SID = "2026-11-09_01"
SID2 = "2026-11-09_02"
SID3 = "2026-11-10_01"
SYSTEM_DATA = b"dummy-system-audio-bytes"
MIC_DATA = b"dummy-mic-audio-bytes"
FILES = [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)]

#: イベントログ1行に現れてよいキー（日付・セッションID・状態・エラー種別）。
ALLOWED_LOG_KEYS = {
    "date",
    "datetime",
    "timestamp",
    "at",
    "time",
    "created_at",
    "session_id",
    "state",
    "error_kind",
}


def _roots(tmp_path):
    staging_root = tmp_path / "staging"
    inbox_root = tmp_path / "inbox"
    work_root = tmp_path / "work"
    for root in (staging_root, inbox_root, work_root):
        root.mkdir(parents=True, exist_ok=True)
    return staging_root, inbox_root, work_root


def _run(store, roots, transcriber, event_log):
    from interview_pipeline.runner import run_once

    staging_root, inbox_root, work_root = roots
    return run_once(
        store,
        staging_root=staging_root,
        inbox_root=inbox_root,
        work_root=work_root,
        transcriber=transcriber,
        event_log=event_log,
    )


def _put_inbox_files(filekit, inbox_root, session_id):
    for name, data in FILES:
        filekit.write(inbox_root / session_id / name, data)


def _read_log_lines(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return [
        line
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# イベントログ
# ---------------------------------------------------------------------------


def test_event_log_appends_one_json_line_per_record(make_event_log, state_of, tmp_path):
    """record は JSONL に1行ずつ追記する。"""
    log_path = tmp_path / "events.jsonl"
    event_log = make_event_log(log_path)
    event_log.record(SID, state_of("received"))
    event_log.record(SID, state_of("needs_reprocess"), error_kind="dummy_error")

    lines = _read_log_lines(log_path)
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert record  # 空でない dict
    assert SID in lines[0]
    assert "received" in lines[0]
    assert "dummy_error" in lines[1]


def test_event_log_records_only_allowed_fields(make_event_log, state_of, tmp_path):
    """1行の記録項目は日付・セッションID・状態・エラー種別のみ。"""
    log_path = tmp_path / "events.jsonl"
    event_log = make_event_log(log_path)
    event_log.record(
        SID, state_of("needs_reprocess"), error_kind="transfer_verification_failed"
    )

    (line,) = _read_log_lines(log_path)
    record = json.loads(line)
    assert (
        set(record.keys()) <= ALLOWED_LOG_KEYS
    ), f"許可されていない記録項目があります: {set(record.keys())}"
    values = {str(v) for v in record.values()}
    assert SID in values
    assert "needs_reprocess" in values
    assert "transfer_verification_failed" in values


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------


def test_run_processes_received_session_to_awaiting_editing(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """received は文字起こし→合成→作業用テキスト生成を経て awaiting_editing。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1], SID)
    log_path = tmp_path / "events.jsonl"

    report = _run(store, roots, make_transcriber(), make_event_log(log_path))

    session = store.get(SID)
    assert state_name(session.state) == "awaiting_editing"
    assert session.processing_completed is True
    transcript = roots[2] / SID / "transcript.md"
    assert transcript.exists()
    content = transcript.read_text(encoding="utf-8")
    assert "SYSTEM-DUMMY-TEXT" in content
    assert "MIC-DUMMY-TEXT" in content
    assert SID in report.processed
    assert SID not in report.failed


def test_run_verifies_transferring_session_with_manifest(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """transferring はマニフェスト検証で昇格する（received 以降へ進む）。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    roots = _roots(tmp_path)
    for name, data in FILES:
        filekit.write(roots[0] / SID / name, data)
    filekit.write_manifest(
        roots[0] / SID / "manifest.json", filekit.manifest_dict(SID, FILES)
    )

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    session = store.get(SID)
    assert state_name(session.state) in ("received", "awaiting_editing")
    assert SID not in report.failed
    inbox_files = {p.name for p in (roots[1] / SID).rglob("*") if p.is_file()}
    assert inbox_files == {"system_track.m4a", "mic_track.m4a"}


def test_run_transferring_with_bad_manifest_becomes_needs_reprocess(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """検証不一致の transferring は needs_reprocess になりログに残る。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    roots = _roots(tmp_path)
    for name, data in FILES:
        filekit.write(roots[0] / SID / name, data)
    manifest = filekit.manifest_dict(SID, FILES)
    manifest["files"][0]["sha256"] = "0" * 64
    filekit.write_manifest(roots[0] / SID / "manifest.json", manifest)
    log_path = tmp_path / "events.jsonl"

    report = _run(store, roots, make_transcriber(), make_event_log(log_path))

    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_verification_failed"
    assert SID in report.failed
    assert _read_log_lines(log_path), "失敗はイベントログに記録される"


def test_run_scans_all_sessions(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """全件走査: 複数の received が1ランでまとめて処理される。"""
    store = make_store()
    roots = _roots(tmp_path)
    for sid in (SID, SID2):
        store.register(sid, t0)
        drive_to(store, sid, "received")
        _put_inbox_files(filekit, roots[1], sid)

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    assert set(report.processed) == {SID, SID2}
    for sid in (SID, SID2):
        assert state_name(store.get(sid).state) == "awaiting_editing"


def test_run_leaves_unprocessable_states_untouched(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """処理対象外の状態（recorded / awaiting_review 等）は変化しない。"""
    store = make_store()
    store.register(SID, t0)  # recorded のまま
    store.register(SID2, t0)
    drive_to(store, SID2, "awaiting_review")
    roots = _roots(tmp_path)

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    assert state_name(store.get(SID).state) == "recorded"
    assert state_name(store.get(SID2).state) == "awaiting_review"
    assert SID not in report.processed
    assert SID2 not in report.processed
    assert SID in report.skipped or SID not in report.failed
    assert SID2 in report.skipped or SID2 not in report.failed


# ---------------------------------------------------------------------------
# 失敗系: 隔離・再試行・STT未接続
# ---------------------------------------------------------------------------


def test_run_failure_isolated_to_failing_session(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """1件失敗しても他件の処理は止まらず、失敗件は needs_reprocess。"""
    store = make_store()
    roots = _roots(tmp_path)
    for sid in (SID, SID2):
        store.register(sid, t0)
        drive_to(store, sid, "received")
        _put_inbox_files(filekit, roots[1], sid)
    log_path = tmp_path / "events.jsonl"
    transcriber = make_transcriber(fail_marker=SID2)

    report = _run(store, roots, transcriber, make_event_log(log_path))

    assert state_name(store.get(SID).state) == "awaiting_editing"
    assert SID in report.processed

    failed_session = store.get(SID2)
    assert state_name(failed_session.state) == "needs_reprocess"
    assert state_name(failed_session.resume_state) == "received"
    assert failed_session.error_kind  # エラー種別が記録される
    assert SID2 in report.failed
    assert SID2 not in report.processed

    lines = _read_log_lines(log_path)
    assert any(SID2 in line for line in lines), "失敗がログに記録される"


def test_run_failure_log_contains_no_content(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """失敗ログに本文や例外メッセージ等の内容が含まれない。"""
    secret = "-".join(["SECRET", "CANARY", "MUST", "NOT", "APPEAR"])
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1], SID)
    log_path = tmp_path / "events.jsonl"
    transcriber = make_transcriber(fail_marker=SID, fail_message=secret)

    _run(store, roots, transcriber, make_event_log(log_path))

    lines = _read_log_lines(log_path)
    assert lines, "失敗は必ず記録される（silent failure 禁止）"
    joined = "\n".join(lines)
    assert secret not in joined
    assert "SYSTEM-DUMMY-TEXT" not in joined
    assert "MIC-DUMMY-TEXT" not in joined
    for line in lines:
        record = json.loads(line)
        assert set(record.keys()) <= ALLOWED_LOG_KEYS


def test_needs_reprocess_session_is_retried_next_run(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """needs_reprocess の件は次のランで再び対象になり、成功すれば進む。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1], SID)
    log_path = tmp_path / "events.jsonl"
    event_log = make_event_log(log_path)

    first = _run(store, roots, make_transcriber(fail_marker=SID), event_log)
    assert state_name(store.get(SID).state) == "needs_reprocess"
    assert SID in first.failed

    second = _run(store, roots, make_transcriber(), event_log)
    assert state_name(store.get(SID).state) == "awaiting_editing"
    assert SID in second.processed


def test_needs_reprocess_has_no_retry_limit(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """再試行に上限はなく、失敗し続ける件は毎ラン対象であり続ける。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1], SID)
    event_log = make_event_log(tmp_path / "events.jsonl")
    transcriber = make_transcriber(fail_marker=SID)

    for _ in range(3):
        report = _run(store, roots, transcriber, event_log)
        assert SID in report.failed
        assert SID not in report.processed
        assert state_name(store.get(SID).state) == "needs_reprocess"


def test_run_with_unavailable_transcriber_never_succeeds(
    make_store, drive_to, make_event_log, filekit, state_name, t0, tmp_path
):
    """STT未接続では成功扱いにならず needs_reprocess になる。"""
    from interview_pipeline.transcribe import UnavailableTranscriber

    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1], SID)
    log_path = tmp_path / "events.jsonl"

    report = _run(store, roots, UnavailableTranscriber(), make_event_log(log_path))

    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.state) != "awaiting_editing"
    assert SID not in report.processed
    assert SID in report.failed
    assert session.error_kind
    assert _read_log_lines(log_path), "STT未接続の失敗も通知・記録される"
    transcript = roots[2] / SID / "transcript.md"
    assert not transcript.exists()


def test_run_with_unavailable_transcriber_error_kind_is_stt_not_configured(
    make_store, drive_to, make_event_log, filekit, t0, tmp_path
):
    """STT未設定時の error_kind は手順書の表記 stt_not_configured。"""
    from interview_pipeline.transcribe import UnavailableTranscriber

    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1], SID)

    _run(
        store,
        roots,
        UnavailableTranscriber(),
        make_event_log(tmp_path / "events.jsonl"),
    )

    assert store.get(SID).error_kind == "stt_not_configured"
