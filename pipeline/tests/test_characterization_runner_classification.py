"""特性テスト: C-057 runner の音声トラック分類（C-136 で接頭辞一致へ適合、
C-142 で分類外ファイルはエラーで停止へ仕様変更）、
C-050 イベントログ同形性。

根拠: refactoring-candidates-2026-11-07.md の interview-notes 候補7
（音声トラック分類と話者ラベル。SPEC・マニフェストの定義は「接頭辞」
だが実装は「部分一致」で、分類外ファイルの扱いを assert するテストが
無い。実エンジン接続の前に現状固定が必須）、候補1（run_once の
検証ブロック重複。抽出前に「2分岐でイベントログが同形に出るか」を
固定するテストがない）、候補3（文字列状態での現挙動が薄い）。

文書との関係: docs/状態管理.md「話者マッピング」は系統をファイル名で
区別するとし、SPEC は分類を「接頭辞」と書く。当初の runner の分類は
ファイル名への部分一致（"system" / "mic" を含むか）であり、C-058 では
その部分一致挙動を固定していた。本人の決定により C-136 で実装を正本に
合わせる（分類を部分一致から接頭辞一致にする）ことになったため、
本ファイルの分類に関するテストは適合後の挙動（接頭辞一致）を固定する。

分類外ファイルの扱い（C-142、仕様由来の更新）: 分類外ファイル
（接頭辞が system でも mic でもないファイル）の扱いは当初 SPEC・正本に
定義がなく、C-058／C-136 では現行の扱い（文字起こしして system 側＝
話者Bとして時系列合成に含める）を維持していた。本人の決定
（2026-11-07、C-142「C-136残件」）により、この扱いを仕様として変更する:
inbox 内に分類外ファイルが1件でも存在するセッションはエラーで停止し、
失敗（RunReport.failed）・状態 needs_reprocess（resume_state は
received）・error_kind は新値 "unclassified_track_file" となり、
イベントログにその失敗が1件記録され、分類済みファイルも含め
文字起こしは一切行わず transcript は作らない。他セッションは止めず、
再処理（needs_reprocess・resume: received）でも分類外ファイルが
残っていれば同じ error_kind で再び失敗する。本ファイルの分類外に
関するテスト4件（test_unclassified_file_is_labelled_as_speaker_b /
test_unclassified_file_mixed_with_classified_files /
test_classification_is_prefix_not_substring /
test_non_prefix_names_with_date_or_take_are_unclassified）は、
この本人の決定に基づく「仕様由来」の更新として新挙動に書き換えた
ものであり、実装都合による書き換えではない。関数名は旧挙動時代の
ものを履歴追跡のため残している箇所があるが、assert は新仕様に従う。

既存テストで固定済みの範囲（本ファイルでは重複させない）:
test_runner.py が system/mic の通常2ファイルの合成・失敗隔離・再試行を、
test_characterization_error_kind.py が各失敗経路の error_kind 値を固定済み。

本ファイルが固定するのは:
- 分類外ファイルが存在するセッションはエラーで停止すること
  （failed／needs_reprocess／resume: received／
  error_kind="unclassified_track_file"／イベントログ1件／
  文字起こし未実施／transcript 未生成。仕様1〜4項、C-142）
- 分類外ファイルと分類済みファイルが混在しても、分類済みファイルも
  含め文字起こしを一切行わないこと（仕様4項、C-142）
- 接頭辞一致による分類（接頭辞でない位置に system/mic を含む名前は
  どちらの系統にも分類されず、分類外として上記の失敗になること）
- system/mic の両方を含む名前でも、接頭辞の系統にだけ分類され、
  文字起こしは1回だけで一方の話者としてのみ現れること（変わらない挙動）
- 日付等で始まる名前（接頭辞でない位置に system/mic を含む名前）が
  分類外として上記の失敗になること
- 分類外ファイルによる失敗が他セッションを止めないこと（仕様5項、C-142）
- 再処理経路でも分類外ファイルが残っていれば同じ error_kind で
  再び失敗すること（仕様6項、C-142）
- サブディレクトリ内のファイルも処理対象になること（変わらない挙動）
- 音声ファイルが1件も無い received が「成功」し空の transcript を作ること
  （変わらない挙動。エラーになるのは分類外ファイルが存在する場合のみ）
- transferring 失敗と needs_reprocess 再開失敗のイベントログの同形性、
  および transferring 成功と received 成功のイベントログの同形性
- 文字列状態（正常値／未知値）での run_once の正規化と skipped 扱い
"""

from __future__ import annotations

import json
from pathlib import Path

SID = "2026-11-09_01"
SID2 = "2026-11-09_02"
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


def _assert_unclassified_failure(
    store, report, roots, transcriber, log_path, state_name, session_id=SID
):
    """C-142 仕様1〜4項の共通 assert（分類外ファイルによる失敗）。

    - RunReport.failed に入る（仕様1項）
    - 状態 needs_reprocess・resume_state は received・
      error_kind は "unclassified_track_file"（仕様2項）
    - イベントログは当該セッションについてこの1件だけで、
      state=needs_reprocess・error_kind="unclassified_track_file"
      （仕様3項）
    - transcriber は呼ばれず、transcript.md は作られない（仕様4項）
    """
    assert session_id in report.failed
    assert session_id not in report.processed
    session = store.get(session_id)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "received"
    assert session.error_kind == "unclassified_track_file"
    assert transcriber.calls == []
    assert not (roots[2] / session_id / "transcript.md").exists()
    records = [
        record
        for record in _log_records(log_path)
        if record.get("session_id") == session_id
    ]
    assert len(records) == 1
    assert records[0]["state"] == "needs_reprocess"
    assert records[0]["error_kind"] == "unclassified_track_file"


# ---------------------------------------------------------------------------
# トラック分類（接頭辞一致は C-136、分類外の失敗化は C-142）
# ---------------------------------------------------------------------------


def test_unclassified_file_is_labelled_as_speaker_b(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """C-142 仕様由来の更新: 分類外ファイルのみのセッションは失敗する。

    旧挙動（C-058／C-136）では system/mic を含まないファイルの発話は
    話者B（system側）として合成されていた。本人の決定（2026-11-07、
    C-142）により、分類外ファイルが1件でもあればセッションはエラーで
    停止する（仕様1〜4項）。関数名は旧挙動時代のものを履歴追跡のため
    残すが、assert は新仕様に従う。
    """
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    filekit.write(roots[1] / SID / "notes.m4a", b"dummy")
    log_path = tmp_path / "e.jsonl"
    transcriber = make_transcriber()

    report = _run(store, roots, transcriber, make_event_log(log_path))

    _assert_unclassified_failure(
        store, report, roots, transcriber, log_path, state_name
    )


def test_unclassified_file_mixed_with_classified_files(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """C-142: 分類済みファイルと混在してもセッション全体が失敗する。

    旧挙動では分類外ファイルは system 側に寄せられ時系列順に B として
    並んでいた。新仕様では、分類外ファイルが混在する場合は分類済み
    ファイルも含め文字起こしを一切行わない（仕様4項）。仕様1〜3項も
    併せて固定する。
    """
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    for name, data in FILES:
        filekit.write(roots[1] / SID / name, data)
    filekit.write(roots[1] / SID / "notes.m4a", b"dummy")
    log_path = tmp_path / "e.jsonl"
    transcriber = make_transcriber()

    report = _run(store, roots, transcriber, make_event_log(log_path))

    _assert_unclassified_failure(
        store, report, roots, transcriber, log_path, state_name
    )


def test_classification_is_prefix_not_substring(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """C-142: 接頭辞でない位置に system/mic を含む名前は分類外で失敗する。

    SPEC の分類は接頭辞によるため、asystemb.m4a / xmicx.m4a はどちらの
    系統にも入らない。旧挙動では分類外として話者B側に現れたが、
    新仕様（C-142）では分類外ファイルの存在によりセッションはエラーで
    停止し、文字起こしは一切行われない（仕様1〜4項）。
    """
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    filekit.write(roots[1] / SID / "asystemb.m4a", b"dummy")
    filekit.write(roots[1] / SID / "xmicx.m4a", b"dummy")
    log_path = tmp_path / "e.jsonl"

    transcriber = make_transcriber()
    report = _run(store, roots, transcriber, make_event_log(log_path))

    _assert_unclassified_failure(
        store, report, roots, transcriber, log_path, state_name
    )


def test_system_mic_filename_is_transcribed_once_as_speaker_b(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """system_mic.m4a は接頭辞 system で system 系統にだけ分類される。

    名前に mic も含むが接頭辞一致では mic 系統には入らないため、
    文字起こしは1回だけで、話者Bとしてのみ現れる。
    """
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    filekit.write(roots[1] / SID / "system_mic.m4a", b"dummy")

    transcriber = make_transcriber()
    _run(store, roots, transcriber, make_event_log(tmp_path / "e.jsonl"))

    assert len(transcriber.calls) == 1
    assert _transcript(roots[2]) == "B: SYSTEM-DUMMY-TEXT\n"


def test_mic_system_filename_is_transcribed_once_as_speaker_a(
    make_store, drive_to, make_transcriber, make_event_log, filekit, t0, tmp_path
):
    """mic_system.m4a は接頭辞 mic で mic 系統にだけ分類される。

    名前に system も含むが接頭辞一致では system 系統には入らないため、
    文字起こしは1回だけで、話者Aとしてのみ現れる。ダミーテキストは
    FakeTranscriber が system の部分一致を優先して SYSTEM-DUMMY-TEXT を
    返すため、テキストは SYSTEM-DUMMY-TEXT のままラベルだけが A になる
    （テキスト規則と話者ラベルは独立）。
    """
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    filekit.write(roots[1] / SID / "mic_system.m4a", b"dummy")

    transcriber = make_transcriber()
    _run(store, roots, transcriber, make_event_log(tmp_path / "e.jsonl"))

    assert len(transcriber.calls) == 1
    assert _transcript(roots[2]) == "A: SYSTEM-DUMMY-TEXT\n"


def test_non_prefix_names_with_date_or_take_are_unclassified(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """C-142: 日付等で始まる名前は接頭辞でないため分類外で失敗する。

    実害のあった誤分類パターンの固定: 2026-11-09_system.m4a は名前に
    system を含むが接頭辞ではないため system 系統には入らず、
    take1_mic.m4a も同様に mic 系統には入らない。旧挙動では分類外として
    両方とも話者B側に現れたが、新仕様（C-142）では分類外ファイルの
    存在によりセッションはエラーで停止し、文字起こしは一切行われない
    （仕様1〜4項）。
    """
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    filekit.write(roots[1] / SID / "2026-11-09_system.m4a", b"dummy")
    filekit.write(roots[1] / SID / "take1_mic.m4a", b"dummy")
    log_path = tmp_path / "e.jsonl"

    transcriber = make_transcriber()
    report = _run(store, roots, transcriber, make_event_log(log_path))

    _assert_unclassified_failure(
        store, report, roots, transcriber, log_path, state_name
    )


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


def test_unclassified_failure_is_isolated_to_failing_session(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """C-142 仕様5項: 分類外の失敗は他セッションの処理を止めない。

    SID は分類済みファイルのみで成功し、SID2 は分類外ファイルを含むため
    unclassified_track_file で失敗する。同一ランで両方を走査しても、
    成功側の transcript 生成と awaiting_editing 遷移は通常どおり行われ、
    失敗側は文字起こしされず transcript も作られない。
    """
    store = make_store()
    roots = _roots(tmp_path)
    for sid in (SID, SID2):
        store.register(sid, t0)
        drive_to(store, sid, "received")
    for name, data in FILES:
        filekit.write(roots[1] / SID / name, data)
        filekit.write(roots[1] / SID2 / name, data)
    filekit.write(roots[1] / SID2 / "notes.m4a", b"dummy")
    log_path = tmp_path / "e.jsonl"
    transcriber = make_transcriber()

    report = _run(store, roots, transcriber, make_event_log(log_path))

    # 成功側（隔離の確認）
    assert SID in report.processed
    assert SID not in report.failed
    assert state_name(store.get(SID).state) == "awaiting_editing"
    assert (roots[2] / SID / "transcript.md").exists()
    # 失敗側は仕様1〜4項どおり（呼び出しは成功側の2件のみ）
    assert SID2 in report.failed
    assert SID2 not in report.processed
    failed_session = store.get(SID2)
    assert state_name(failed_session.state) == "needs_reprocess"
    assert state_name(failed_session.resume_state) == "received"
    assert failed_session.error_kind == "unclassified_track_file"
    assert not (roots[2] / SID2 / "transcript.md").exists()
    assert len(transcriber.calls) == 2
    assert all(SID2 not in call for call in transcriber.calls)
    failed_records = [
        record for record in _log_records(log_path) if record.get("session_id") == SID2
    ]
    assert len(failed_records) == 1
    assert failed_records[0]["state"] == "needs_reprocess"
    assert failed_records[0]["error_kind"] == "unclassified_track_file"


def test_unclassified_failure_repeats_on_reprocess(
    make_store,
    drive_to,
    make_transcriber,
    make_event_log,
    filekit,
    state_name,
    t0,
    tmp_path,
):
    """C-142 仕様6項: 再処理でも分類外が残れば同じ error_kind で再失敗。

    1回目のランで needs_reprocess（resume: received）になったセッションを
    そのまま2回目のランで再処理しても、分類外ファイルが残っている限り
    再び unclassified_track_file で失敗し、文字起こしは行われず
    transcript も作られない。各ランのイベントログは当該セッションに
    ついて1件ずつ、同形で記録される。
    """
    store = _received_store(make_store, drive_to, t0)
    roots = _roots(tmp_path)
    for name, data in FILES:
        filekit.write(roots[1] / SID / name, data)
    filekit.write(roots[1] / SID / "notes.m4a", b"dummy")
    log_path = tmp_path / "e.jsonl"
    event_log = make_event_log(log_path)

    first_transcriber = make_transcriber()
    first = _run(store, roots, first_transcriber, event_log)
    _assert_unclassified_failure(
        store, first, roots, first_transcriber, log_path, state_name
    )

    second_transcriber = make_transcriber()
    second = _run(store, roots, second_transcriber, event_log)

    assert SID in second.failed
    assert SID not in second.processed
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "received"
    assert session.error_kind == "unclassified_track_file"
    assert second_transcriber.calls == []
    assert not (roots[2] / SID / "transcript.md").exists()
    records = [
        record for record in _log_records(log_path) if record.get("session_id") == SID
    ]
    assert len(records) == 2
    for record in records:
        assert record["state"] == "needs_reprocess"
        assert record["error_kind"] == "unclassified_track_file"


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
