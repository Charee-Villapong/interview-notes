"""特性テスト（現状固定）: C-057 runner の音声トラック分類、C-050 イベントログ同形性。

根拠: refactoring-candidates-2026-11-07.md の interview-notes 候補7
（音声トラック分類と話者ラベル。SPEC・マニフェストの定義は「接頭辞」
だが実装は「部分一致」で、分類外ファイルの扱いを assert するテストが
無い。実エンジン接続の前に現状固定が必須）、候補1（run_once の
検証ブロック重複。抽出前に「2分岐でイベントログが同形に出るか」を
固定するテストがない）、候補3（文字列状態での現挙動が薄い）。

文書との関係: docs/状態管理.md「話者マッピング」は系統をファイル名で
区別するとし、SPEC は分類を「接頭辞」と書くが、現行 runner の分類は
ファイル名への部分一致（"system" / "mic" を含むか）である。本ファイルは
現行の部分一致挙動を固定する（文書側の是非はリファクタとは混ぜない）。

既存テストで固定済みの範囲（本ファイルでは重複させない）:
test_runner.py が system/mic の通常2ファイルの合成・失敗隔離・再試行を、
test_characterization_error_kind.py が各失敗経路の error_kind 値を固定済み。

本ファイルが固定するのは:
- 分類外ファイル（system/mic を含まない名前）のセグメントの行き先
  （話者B＝system 側に寄せられる）と、混在時の並び
- 部分一致による分類（接頭辞でない位置に system/mic を含む名前）
- 両方の文字列を含むファイルが二重に文字起こしされ両話者で現れること
- サブディレクトリ内のファイルも処理対象になること
- 音声ファイルが1件も無い received が「成功」し空の transcript を作ること
- transferring 失敗と needs_reprocess 再開失敗のイベントログの同形性、
  および transferring 成功と received 成功のイベントログの同形性
- 文字列状態（正常値／未知値）での run_once の正規化と skipped 扱い
"""

from __future__ import annotations

import json
from pathlib import Path

SID = "2026-11-09_01"
SYSTEM_DATA = b"dummy-system-audio-bytes"
MIC_DATA = b"dummy-mic-audio-bytes"
FILES = [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)]


def _roots(tmp_path, label=""):
    staging_root = tmp_path / f"staging{label}"
    inbox_root = tmp_path / f"inbox{label}"
    work_root = tmp_path / f"work{label}"
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


def _received_store(make_store, drive_to, t0):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    return store


def _transcript(work_root: Path) -> str:
    return (work_root / SID / "transcript.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# トラック分類（部分一致と分類外）
# ---------------------------------------------------------------------------


def test_unclassified_file_is_labelled_as_speaker_b(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """system/mic を含まないファイルの発話は話者B（system側）になる。"""
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    filekit.write(roots[1] / SID / "notes.m4a", b"dummy")

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    assert SID in report.processed
    assert _transcript(roots[2]) == "B: GENERIC-DUMMY-TEXT\n"


def test_unclassified_file_mixed_with_classified_files(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """分類外ファイルは system 側に寄せられ、時系列順に B として並ぶ。"""
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    for name, data in FILES:
        filekit.write(roots[1] / SID / name, data)
    filekit.write(roots[1] / SID / "notes.m4a", b"dummy")

    _run(store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl"))

    assert _transcript(roots[2]) == (
        "B: SYSTEM-DUMMY-TEXT\n" "B: GENERIC-DUMMY-TEXT\n" "A: MIC-DUMMY-TEXT\n"
    )


def test_classification_is_substring_not_prefix(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """現行挙動: 接頭辞でなく名前に含むだけでも system/mic に分類される。"""
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    filekit.write(roots[1] / SID / "asystemb.m4a", b"dummy")
    filekit.write(roots[1] / SID / "xmicx.m4a", b"dummy")

    _run(store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl"))

    assert _transcript(roots[2]) == "B: SYSTEM-DUMMY-TEXT\nA: MIC-DUMMY-TEXT\n"


def test_file_matching_both_substrings_is_transcribed_twice(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """system と mic の両方を含む名前は両系統に入り、二重に現れる。"""
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    filekit.write(roots[1] / SID / "system_mic.m4a", b"dummy")

    _run(store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl"))

    assert _transcript(roots[2]) == "B: SYSTEM-DUMMY-TEXT\nA: SYSTEM-DUMMY-TEXT\n"


def test_files_in_subdirectories_are_processed(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """inbox のサブディレクトリ内にあるファイルも処理対象になる。"""
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    for name, data in FILES:
        filekit.write(roots[1] / SID / "sub" / name, data)

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    assert SID in report.processed
    assert _transcript(roots[2]) == "B: SYSTEM-DUMMY-TEXT\nA: MIC-DUMMY-TEXT\n"


def test_received_without_any_files_succeeds_with_empty_transcript(
    make_store, drive_to, make_transcriber, make_event_log, state_name, t0, tmp_path
):
    """現行挙動: 音声が無くても処理は成功し、空の transcript が作られる。"""
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    assert SID in report.processed
    assert state_name(store.get(SID).state) == "awaiting_editing"
    transcript_path = roots[2] / SID / "transcript.md"
    assert transcript_path.exists()
    assert transcript_path.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# イベントログの同形性（run_once の2つの検証分岐、C-050 の安全網）
# ---------------------------------------------------------------------------


def _log_records(log_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _put_staging(filekit, staging_root, bad_sha=False):
    for name, data in FILES:
        filekit.write(staging_root / SID / name, data)
    manifest = filekit.manifest_dict(SID, FILES)
    if bad_sha:
        manifest["files"][0]["sha256"] = "0" * 64
    filekit.write_manifest(staging_root / SID / "manifest.json", manifest)


def _strip_dates(records: list[dict]) -> list[dict]:
    return [
        {key: value for key, value in record.items() if key != "date"}
        for record in records
    ]


def test_failure_event_log_shape_matches_between_verify_branches(
    make_store,
    drive_to,
    state_of,
    make_transcriber,
    make_event_log,
    filekit,
    t0,
    tmp_path,
):
    """transferring 失敗と再開分岐の失敗で、イベントログの形が一致する。

    run_once 内の検証ブロックは2分岐に重複している（候補1）。抽出で
    片方だけ形が変わらないよう、キー集合と値（date を除く）を両分岐で
    突き合わせて固定する。
    """
    records_by_branch = {}
    for label, branch in (("a", "transferring"), ("b", "reprocess")):
        store = make_store(name=f"sessions-{label}.json")
        store.register(SID, t0)
        drive_to(store, SID, "transferring")
        if branch == "reprocess":
            store.transition(
                SID,
                state_of("needs_reprocess"),
                error_kind="transfer_verification_failed",
                resume_state=state_of("transfer_pending"),
            )
        roots = _roots(tmp_path, label=label)
        _put_staging(filekit, roots[0], bad_sha=True)
        log_path = tmp_path / f"events-{label}.jsonl"
        report = _run(store, roots, make_transcriber(), make_event_log(log_path))
        assert SID in report.failed
        records_by_branch[branch] = _log_records(log_path)

    for records in records_by_branch.values():
        assert len(records) == 1
        assert set(records[0].keys()) == {
            "date",
            "session_id",
            "state",
            "error_kind",
        }
    assert _strip_dates(records_by_branch["transferring"]) == _strip_dates(
        records_by_branch["reprocess"]
    )


def test_success_event_log_shape_matches_between_verify_branches(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    t0,
    tmp_path,
):
    """transferring 成功（検証→処理）と received 成功のログが同形。

    どちらの経路でも記録は awaiting_editing の1件だけで、
    error_kind は null になる。
    """
    records_by_branch = {}
    # transferring 経路
    store = make_store(name="sessions-t.json")
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    roots = _roots(tmp_path, label="t")
    _put_staging(filekit, roots[0])
    log_path = tmp_path / "events-t.jsonl"
    _run(store, roots, make_transcriber(), make_event_log(log_path))
    records_by_branch["transferring"] = _log_records(log_path)
    # received 経路
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path, label="r")
    for name, data in FILES:
        filekit.write(roots[1] / SID / name, data)
    log_path = tmp_path / "events-r.jsonl"
    _run(store, roots, make_transcriber(), make_event_log(log_path))
    records_by_branch["received"] = _log_records(log_path)

    for records in records_by_branch.values():
        assert len(records) == 1
        assert set(records[0].keys()) == {
            "date",
            "session_id",
            "state",
            "error_kind",
        }
        assert records[0]["state"] == "awaiting_editing"
        assert records[0]["error_kind"] is None
    assert _strip_dates(records_by_branch["transferring"]) == _strip_dates(
        records_by_branch["received"]
    )


# ---------------------------------------------------------------------------
# 文字列状態での run_once（状態の文字列化、C-052 の安全網）
# ---------------------------------------------------------------------------


def test_run_once_normalizes_plain_string_state(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """状態が State でなく文字列 "received" でも正規化されて処理される。"""
    store = _received_store(make_store, drive_to, t0)
    store.get(SID).state = "received"  # あえて文字列のままにする
    roots = _roots(tmp_path)
    for name, data in FILES:
        filekit.write(roots[1] / SID / name, data)

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    assert SID in report.processed
    assert state_name(store.get(SID).state) == "awaiting_editing"


def test_run_once_skips_unknown_string_state(
    make_store, make_transcriber, make_event_log, t0, tmp_path
):
    """解釈できない文字列状態は skipped になり、状態は変更されない。"""
    store = make_store()
    store.register(SID, t0)
    store.get(SID).state = "bogus_state"
    roots = _roots(tmp_path)

    report = _run(
        store, roots, make_transcriber(), make_event_log(tmp_path / "e.jsonl")
    )

    assert SID in report.skipped
    assert SID not in report.processed
    assert SID not in report.failed
    assert store.get(SID).state == "bogus_state"
