"""特性テスト（現状固定）: C-054 ``error_kind`` 文字列値。

根拠: refactoring-candidates-2026-11-07.md の interview-notes 候補5
（``error_kind`` リテラルの散在。定数化の前に現行値を固定する）、
pipeline/SPEC.md の ``receive.py`` / ``runner.py`` 契約、
docs/手順書.md「STTエンジン」（未設定時は ``stt_not_configured``）。

既存テストで固定済みの範囲（本ファイルでは重複させない）:
- ``transfer_verification_failed`` のセッション側の値は
  test_receive.py の SHA-256／サイズ／欠落の各テストで固定済み。
- ``stt_not_configured`` のセッション側の値は test_runner.py の
  専用テストで固定済み。
- ``audio_content_detected`` のセッション側の値は
  test_saving.py で固定済み。

本ファイルが固定するのは、既存テストが真偽（truthy）確認のみ、
または存在確認のみで、文字列値そのものを固定していない経路:
- 汎用処理失敗の ``processing_failed``（セッションとイベントログ）
- 各失敗経路のイベントログ側 ``error_kind`` の値
- マニフェスト自体が読めない場合の種別
- ``needs_reprocess`` からの再開分岐（第2の検証・失敗経路）での種別
"""

from __future__ import annotations

import json
from pathlib import Path

SID = "2026-11-09_01"
SYSTEM_DATA = b"dummy-system-audio-bytes"
MIC_DATA = b"dummy-mic-audio-bytes"
FILES = [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)]


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


def _put_inbox_files(filekit, inbox_root, session_id=SID):
    for name, data in FILES:
        filekit.write(inbox_root / session_id / name, data)


def _put_staging(filekit, staging_root, bad_sha=False, session_id=SID):
    for name, data in FILES:
        filekit.write(staging_root / session_id / name, data)
    manifest = filekit.manifest_dict(session_id, FILES)
    if bad_sha:
        manifest["files"][0]["sha256"] = "0" * 64
    filekit.write_manifest(staging_root / session_id / "manifest.json", manifest)


def _log_records(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _log_error_kinds(log_path: Path) -> list:
    return [record.get("error_kind") for record in _log_records(log_path)]


def _received_store(make_store, drive_to, t0):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    return store


# ---------------------------------------------------------------------------
# 汎用処理失敗: processing_failed（既存は truthy 確認のみ）
# ---------------------------------------------------------------------------


def test_generic_processing_failure_error_kind_is_processing_failed(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """文字起こしの汎用失敗ではセッション・ログとも processing_failed。"""
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1])
    log_path = tmp_path / "events.jsonl"

    report = _run(
        store, roots, make_transcriber(fail_marker=SID), make_event_log(log_path)
    )

    session = store.get(SID)
    assert SID in report.failed
    assert session.error_kind == "processing_failed"
    assert _log_error_kinds(log_path) == ["processing_failed"]


# ---------------------------------------------------------------------------
# イベントログ側の error_kind（既存はセッション側のみ固定）
# ---------------------------------------------------------------------------


def test_stt_not_configured_error_kind_in_event_log(
    make_store, drive_to, make_event_log, filekit, t0, tmp_path
):
    """STT未設定の失敗はイベントログにも stt_not_configured と残る。"""
    from interview_pipeline.transcribe import UnavailableTranscriber

    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1])
    log_path = tmp_path / "events.jsonl"

    _run(store, roots, UnavailableTranscriber(), make_event_log(log_path))

    assert store.get(SID).error_kind == "stt_not_configured"
    assert _log_error_kinds(log_path) == ["stt_not_configured"]


def test_transfer_verification_failed_error_kind_in_event_log(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """transferring の検証失敗はイベントログにも同種別で残る。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    roots = _roots(tmp_path)
    _put_staging(filekit, roots[0], bad_sha=True)
    log_path = tmp_path / "events.jsonl"

    _run(store, roots, make_transcriber(), make_event_log(log_path))

    assert store.get(SID).error_kind == "transfer_verification_failed"
    assert _log_error_kinds(log_path) == ["transfer_verification_failed"]


def test_malformed_manifest_error_kind_is_transfer_verification_failed(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """マニフェスト自体が読めない場合も転送検証失敗の種別になる。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    roots = _roots(tmp_path)
    broken = roots[0] / SID / "manifest.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json", encoding="utf-8")
    log_path = tmp_path / "events.jsonl"

    report = _run(store, roots, make_transcriber(), make_event_log(log_path))

    session = store.get(SID)
    assert SID in report.failed
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_verification_failed"
    assert _log_error_kinds(log_path) == ["transfer_verification_failed"]


# ---------------------------------------------------------------------------
# needs_reprocess からの再開分岐（run_once の第2の検証・失敗経路）
# ---------------------------------------------------------------------------


def _transfer_needs_reprocess_store(make_store, drive_to, state_of, t0):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="transfer_verification_failed",
        resume_state=state_of("transfer_pending"),
    )
    return store


def test_reprocess_transfer_failure_keeps_transfer_verification_failed(
    make_store,
    drive_to,
    state_of,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """再開分岐（resume: transfer_pending）で再び検証に失敗しても同種別。"""
    store = _transfer_needs_reprocess_store(make_store, drive_to, state_of, t0)
    roots = _roots(tmp_path)
    _put_staging(filekit, roots[0], bad_sha=True)
    log_path = tmp_path / "events.jsonl"

    report = _run(store, roots, make_transcriber(), make_event_log(log_path))

    session = store.get(SID)
    assert SID in report.failed
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_verification_failed"
    assert _log_error_kinds(log_path) == ["transfer_verification_failed"]


def _received_needs_reprocess_store(make_store, drive_to, state_of, t0):
    store = _received_store(make_store, drive_to, t0)
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="processing_failed",
        resume_state=state_of("received"),
    )
    return store


def test_reprocess_received_generic_failure_error_kind_is_processing_failed(
    make_store,
    drive_to,
    state_of,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """既に needs_reprocess の件が再び汎用失敗しても processing_failed。"""
    store = _received_needs_reprocess_store(make_store, drive_to, state_of, t0)
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1])
    log_path = tmp_path / "events.jsonl"

    report = _run(
        store, roots, make_transcriber(fail_marker=SID), make_event_log(log_path)
    )

    session = store.get(SID)
    assert SID in report.failed
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "received"
    assert session.error_kind == "processing_failed"
    assert _log_error_kinds(log_path) == ["processing_failed"]


def test_reprocess_received_stt_failure_error_kind_is_stt_not_configured(
    make_store, drive_to, state_of, make_event_log, filekit, t0, tmp_path
):
    """既に needs_reprocess の件が STT未設定で再び失敗しても同種別。"""
    from interview_pipeline.transcribe import UnavailableTranscriber

    store = _received_needs_reprocess_store(make_store, drive_to, state_of, t0)
    roots = _roots(tmp_path)
    _put_inbox_files(filekit, roots[1])
    log_path = tmp_path / "events.jsonl"

    _run(store, roots, UnavailableTranscriber(), make_event_log(log_path))

    assert store.get(SID).error_kind == "stt_not_configured"
    assert _log_error_kinds(log_path) == ["stt_not_configured"]
