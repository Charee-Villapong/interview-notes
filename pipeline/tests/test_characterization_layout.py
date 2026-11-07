"""特性テスト（現状固定）: C-055 レイアウト／ファイル名。

根拠: refactoring-candidates-2026-11-07.md の interview-notes 候補6
（``manifest.json`` / ``transcript.md`` / データ領域配下のサブパスが
複数モジュールに分散。集約の前に現行の配置を固定する）、
pipeline/SPEC.md の決定事項4（マニフェスト配置）と ``runner.py`` 契約
（作業用テキストは ``work/<session_id>/transcript.md``）、
docs/手順書.md「データ領域」（``state/sessions.json`` /
``staging/<session_id>/`` / ``inbox/<session_id>/`` /
``work/<session_id>/`` / ``log/events.jsonl``）。

既存テストで固定済みの範囲（本ファイルでは重複させない）:
- ``work/<session_id>/transcript.md`` の存在は test_runner.py の
  正常系テストで固定済み（失敗時に作られないことも含む）。
- transferring 検証成功時の ``inbox/<session_id>/`` 内のファイル名
  集合は test_runner.py で固定済み。
- 下書き保存先 ``notes_dir/<日付>/<session_id>.md`` は
  test_saving.py で固定済み。

本ファイルが固定するのは:
- 成功時に work 配下へ作られるファイルの完全な集合（余分な
  ファイルが作られないことまでは既存テストは見ていない）
- 検証成功後の staging の残存物（音声は移り、マニフェストだけ残る）
- ``needs_reprocess`` 再開分岐（第2の検証経路）成功時の配置
- マニフェストが無い transferring が何も作らないこと
- CLI（``python -m interview_pipeline``）経由のデータ領域レイアウト
  （cli.py には既存テストが無く、サブパス一式は未固定だった）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SID = "2026-11-09_01"
SYSTEM_DATA = b"dummy-system-audio-bytes"
MIC_DATA = b"dummy-mic-audio-bytes"
FILES = [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)]
AUDIO_NAMES = {"system_track.m4a", "mic_track.m4a"}


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


def _relative_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def _put_staging(filekit, staging_root, session_id=SID):
    for name, data in FILES:
        filekit.write(staging_root / session_id / name, data)
    filekit.write_manifest(
        staging_root / session_id / "manifest.json",
        filekit.manifest_dict(session_id, FILES),
    )


# ---------------------------------------------------------------------------
# runner 経由の配置
# ---------------------------------------------------------------------------


def test_received_success_creates_exactly_transcript_md_in_work(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """received の処理成功で work 配下に作られるのは transcript.md だけ。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    roots = _roots(tmp_path)
    for name, data in FILES:
        filekit.write(roots[1] / SID / name, data)

    _run(store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl"))

    assert _relative_files(roots[2]) == {f"{SID}/transcript.md"}
    # inbox 側の音声はそのまま残る（移動も複製もされない）。
    assert _relative_files(roots[1]) == {f"{SID}/{name}" for name in AUDIO_NAMES}


def test_transferring_success_layout_across_roots(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """検証→処理まで進んだ場合の staging / inbox / work の配置一式。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    roots = _roots(tmp_path)
    _put_staging(filekit, roots[0])

    _run(store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl"))

    assert _relative_files(roots[1]) == {f"{SID}/{name}" for name in AUDIO_NAMES}
    assert _relative_files(roots[2]) == {f"{SID}/transcript.md"}
    # ステージングにはマニフェストだけが残り、音声は inbox へ移る。
    assert _relative_files(roots[0]) == {f"{SID}/manifest.json"}


def test_reprocess_transfer_success_layout_across_roots(
    make_store,
    drive_to,
    state_of,
    make_transcriber,
    make_event_log,
    filekit,
    t0,
    tmp_path,
):
    """再開分岐（resume: transfer_pending）成功時も配置は初回経路と同じ。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="transfer_verification_failed",
        resume_state=state_of("transfer_pending"),
    )
    roots = _roots(tmp_path)
    _put_staging(filekit, roots[0])

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    assert SID in report.processed
    assert _relative_files(roots[1]) == {f"{SID}/{name}" for name in AUDIO_NAMES}
    assert _relative_files(roots[2]) == {f"{SID}/transcript.md"}
    assert _relative_files(roots[0]) == {f"{SID}/manifest.json"}


def test_transferring_without_manifest_creates_no_files(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """manifest.json が無い transferring は対象外で、何も生成されない。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    roots = _roots(tmp_path)

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    assert SID in report.skipped
    assert state_name(store.get(SID).state) == "transferring"
    assert _relative_files(roots[0]) == set()
    assert _relative_files(roots[1]) == set()
    assert _relative_files(roots[2]) == set()


# ---------------------------------------------------------------------------
# CLI 経由のデータ領域レイアウト
# ---------------------------------------------------------------------------


def _cli_env(data_dir: Path, notes_dir: Path) -> dict:
    import interview_pipeline

    src_dir = Path(interview_pipeline.__file__).resolve().parent.parent
    env = dict(os.environ)
    env["INTERVIEW_DATA_DIR"] = str(data_dir)
    env["INTERVIEW_NOTES_DIR"] = str(notes_dir)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{src_dir}{os.pathsep}{pythonpath}" if pythonpath else str(src_dir)
    )
    return env


def _cli(args, data_dir: Path, notes_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "interview_pipeline", *args],
        capture_output=True,
        text=True,
        env=_cli_env(data_dir, notes_dir),
        timeout=60,
    )


def _cli_setup(tmp_path):
    data_dir = tmp_path / "data"
    notes_dir = tmp_path / "notes"
    data_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    return data_dir, notes_dir


def _cli_register_with_staging(data_dir, notes_dir, filekit, bad_sha=False):
    result = _cli(
        ["register", SID, "--created-at", "2026-11-09T09:00:00+00:00"],
        data_dir,
        notes_dir,
    )
    assert result.returncode == 0, result.stderr
    _put_staging(filekit, data_dir / "staging")
    if bad_sha:
        manifest = filekit.manifest_dict(SID, FILES)
        manifest["files"][0]["sha256"] = "0" * 64
        filekit.write_manifest(data_dir / "staging" / SID / "manifest.json", manifest)


def test_cli_register_creates_state_sessions_json(tmp_path, filekit):
    """register は <DATA>/state/sessions.json にセッションを記録する。"""
    data_dir, notes_dir = _cli_setup(tmp_path)

    result = _cli(
        ["register", SID, "--created-at", "2026-11-09T09:00:00+00:00"],
        data_dir,
        notes_dir,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{SID} recorded"
    sessions_file = data_dir / "state" / "sessions.json"
    assert sessions_file.exists()
    payload = json.loads(sessions_file.read_text(encoding="utf-8"))
    entries = payload["sessions"] if isinstance(payload, dict) else payload
    assert [entry["session_id"] for entry in entries] == [SID]


def test_cli_receive_promotes_staging_manifest_to_inbox(tmp_path, filekit):
    """receive は staging/<sid>/manifest.json を読み inbox/<sid>/ へ昇格。"""
    data_dir, notes_dir = _cli_setup(tmp_path)
    _cli_register_with_staging(data_dir, notes_dir, filekit)

    result = _cli(["receive", SID], data_dir, notes_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert _relative_files(data_dir / "inbox") == {
        f"{SID}/{name}" for name in AUDIO_NAMES
    }
    # ステージングにはマニフェストだけが残る。
    assert _relative_files(data_dir / "staging") == {f"{SID}/manifest.json"}
    status = _cli(["status"], data_dir, notes_dir)
    assert f"{SID} received" in status.stdout


def test_cli_receive_failure_keeps_staging_and_creates_no_inbox(tmp_path, filekit):
    """receive 検証失敗時は inbox を作らず、staging の一式が残る。"""
    data_dir, notes_dir = _cli_setup(tmp_path)
    _cli_register_with_staging(data_dir, notes_dir, filekit, bad_sha=True)

    result = _cli(["receive", SID], data_dir, notes_dir)

    assert result.returncode != 0
    assert _relative_files(data_dir / "staging") == {
        f"{SID}/manifest.json",
        *(f"{SID}/{name}" for name in AUDIO_NAMES),
    }
    inbox_root = data_dir / "inbox"
    assert not inbox_root.exists() or _relative_files(inbox_root) == set()
    status = _cli(["status"], data_dir, notes_dir)
    assert f"{SID} needs_reprocess" in status.stdout


def test_cli_run_writes_log_events_jsonl(tmp_path, filekit):
    """run は <DATA>/log/events.jsonl に記録し、失敗時は work を作らない。"""
    data_dir, notes_dir = _cli_setup(tmp_path)
    _cli_register_with_staging(data_dir, notes_dir, filekit)
    received = _cli(["receive", SID], data_dir, notes_dir)
    assert received.returncode == 0, received.stderr

    result = _cli(["run"], data_dir, notes_dir)

    assert result.returncode == 0, result.stderr
    log_file = data_dir / "log" / "events.jsonl"
    assert log_file.exists()
    records = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["session_id"] for record in records] == [SID]
    # CLI の run は STT 未接続で失敗するため作業用テキストは生成されない。
    assert not (data_dir / "work" / SID / "transcript.md").exists()
    status = _cli(["status"], data_dir, notes_dir)
    assert f"{SID} needs_reprocess" in status.stdout
