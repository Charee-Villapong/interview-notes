"""転送プル（pull）のテスト。

根拠: pipeline/SPEC.md「PULL 追加契約（転送のプル方式。2026-11-08 追記）」、
docs/状態管理.md「転送方式（プル方式）」「遷移条件」
「二重処理・二重保存の防止」。

契約の要点:
- 対象は Mac 側の状態が ``recorded``／``transfer_pending``／
  ``needs_reprocess`` のセッションだけ。それ以外はレポートにも数えない。
- VM 側で ``received`` 以降のセッションは再取得せず ``skipped``。
- VM 未登録は Mac の ``created_at`` で登録し、無ければセッションIDの
  日付部分（UTC 0時）で登録する。
- 取得はマニフェスト→掲載ファイルの順でステージングへ配置し、
  既存の ``verify_and_promote`` で検証・昇格する。成功は ``pulled``。
- 取得失敗（接続不可・ファイル欠落・マニフェスト取得失敗・不正）は
  ``transfer_fetch_failed``、検証不一致は
  ``transfer_verification_failed`` として区別し、いずれも当該セッションを
  ``needs_reprocess``（resume: ``transfer_pending``）にして ``failed``
  へ入れる。他件の処理は止めない。
- ``SshMacSource`` は標準出力経由で読み、Mac 形式 JSON
  （``sessionId``／``state``／``createdAt``）を変換する。
- CLI ``pull`` は ``pulled=[...] failed=[...] skipped=[...]`` を表示し、
  失敗があれば終了コード 1。接続情報の環境変数が未設定なら
  ``SystemExit`` で終了する。ソース生成は ``cli._build_mac_source()``。

流儀は既存テスト（test_receive.py 等）に合わせ、``interview_pipeline``
への import はテスト関数・ヘルパー内でのみ行う（未実装でもコレクション
エラーで全滅しない形）。実値は使わずダミーで代用する。
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SID = "2026-11-09_01"
SID2 = "2026-11-09_02"
SID3 = "2026-11-10_01"
SYSTEM_DATA = b"dummy-system-audio-bytes"
MIC_DATA = b"dummy-mic-audio-bytes"
FILES = [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)]
CREATED = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)

#: MacFetchError を探す候補モジュール（SPEC では errors.py が集約先）。
_ERROR_SEARCH_MODULES = (
    "interview_pipeline.pull",
    "interview_pipeline.errors",
    "interview_pipeline",
)


def _mac_fetch_error_type():
    for module_name in _ERROR_SEARCH_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        cls = getattr(module, "MacFetchError", None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            return cls
    raise AssertionError("MacFetchError がパッケージ内に見つかりません")


def _mac_fetch_error(message: str = "dummy fetch failure"):
    return _mac_fetch_error_type()(message)


def _record(session_id, state, created_at=CREATED):
    """MacSessionRecord を作る（import は実行時に行う）。"""
    from interview_pipeline.pull import MacSessionRecord

    return MacSessionRecord(session_id=session_id, state=state, created_at=created_at)


def _manifest_bytes(session_id: str, files) -> bytes:
    payload = {
        "session_id": session_id,
        "files": [
            {
                "name": name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in files
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class FakeMacSource:
    """テスト側で定義する MacSource のフェイク実装（実接続しない）。

    ``files`` は ``(session_id, name) -> bytes``。``manifest.json`` も
    ここから返す。``fail_on`` に入れた ``(session_id, name)`` は
    ``MacFetchError`` で失敗する。``manifest_overrides`` は
    マニフェスト取得結果の差し替え（不正マニフェスト用）。
    """

    def __init__(
        self,
        records,
        files=None,
        *,
        fail_on=(),
        manifest_overrides=None,
        list_error=None,
    ):
        self._records = list(records)
        self._files = dict(files or {})
        self._fail_on = set(fail_on)
        self._manifest_overrides = dict(manifest_overrides or {})
        self._list_error = list_error
        self.fetch_calls: list[tuple[str, str]] = []
        self.list_calls = 0

    def list_sessions(self):
        self.list_calls += 1
        if self._list_error is not None:
            raise self._list_error
        return list(self._records)

    def fetch_file(self, session_id, name):
        self.fetch_calls.append((session_id, name))
        if (session_id, name) in self._fail_on or (session_id, "*") in self._fail_on:
            raise _mac_fetch_error(f"dummy fetch failure: {session_id}/{name}")
        if name == "manifest.json" and session_id in self._manifest_overrides:
            return self._manifest_overrides[session_id]
        try:
            return self._files[(session_id, name)]
        except KeyError:
            raise _mac_fetch_error(f"dummy missing file: {session_id}/{name}") from None


def _good_files(session_id, files=FILES):
    """正常なセッション1件分の取得ファイル（マニフェスト含む）。"""
    result = {(session_id, name): data for name, data in files}
    result[(session_id, "manifest.json")] = _manifest_bytes(session_id, files)
    return result


def _roots(tmp_path):
    staging_root = tmp_path / "staging"
    inbox_root = tmp_path / "inbox"
    staging_root.mkdir(parents=True, exist_ok=True)
    inbox_root.mkdir(parents=True, exist_ok=True)
    return staging_root, inbox_root


def _pull(store, source, roots, event_log):
    from interview_pipeline.pull import pull_pending

    staging_root, inbox_root = roots
    return pull_pending(
        store,
        source,
        staging_root=staging_root,
        inbox_root=inbox_root,
        event_log=event_log,
    )


def _read_log_records(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _inbox_files(inbox_root: Path, session_id: str) -> dict[str, bytes]:
    base = inbox_root / session_id
    if not base.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in base.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# 契約値・データクラス
# ---------------------------------------------------------------------------


def test_error_kind_transfer_fetch_failed_constant():
    """取得失敗の種別は transfer_fetch_failed で、検証失敗と区別される。"""
    from interview_pipeline import states

    assert states.ERROR_KIND_TRANSFER_FETCH_FAILED == "transfer_fetch_failed"
    assert (
        states.ERROR_KIND_TRANSFER_FETCH_FAILED
        != states.ERROR_KIND_TRANSFER_VERIFICATION_FAILED
    )


def test_mac_session_record_and_pull_report_fields():
    """MacSessionRecord と PullReport は契約どおりのフィールドを持つ。"""
    from interview_pipeline.pull import PullReport

    record = _record(SID, "transfer_pending", CREATED)
    assert record.session_id == SID
    assert record.state == "transfer_pending"
    assert record.created_at == CREATED

    record_without_date = _record(SID, "recorded", None)
    assert record_without_date.created_at is None

    report = PullReport()
    assert report.pulled == []
    assert report.failed == []
    assert report.skipped == []


# ---------------------------------------------------------------------------
# pull_pending: 正常系
# ---------------------------------------------------------------------------


def test_pull_transfer_pending_promotes_to_received(
    make_store, make_event_log, state_name, tmp_path
):
    """transfer_pending のセッションが取得→検証→inbox 昇格し received。"""
    store = make_store()
    source = FakeMacSource([_record(SID, "transfer_pending")], _good_files(SID))
    roots = _roots(tmp_path)
    log_path = tmp_path / "events.jsonl"

    report = _pull(store, source, roots, make_event_log(log_path))

    assert report.pulled == [SID]
    assert report.failed == []
    assert report.skipped == []
    assert state_name(store.get(SID).state) == "received"
    # inbox へ中身そのままで昇格していること。
    assert _inbox_files(roots[1], SID) == {
        "system_track.m4a": SYSTEM_DATA,
        "mic_track.m4a": MIC_DATA,
    }
    # 取得順はマニフェストが先（契約: マニフェスト→掲載ファイルの順）。
    assert source.fetch_calls[0] == (SID, "manifest.json")
    assert {name for _, name in source.fetch_calls} == {
        "manifest.json",
        "system_track.m4a",
        "mic_track.m4a",
    }


@pytest.mark.parametrize(
    "mac_state", ["recorded", "transfer_pending", "needs_reprocess"]
)
def test_pull_targets_all_transfer_waiting_mac_states(
    make_store, make_event_log, state_name, tmp_path, mac_state
):
    """Mac 側が recorded／transfer_pending／needs_reprocess なら対象。"""
    store = make_store()
    source = FakeMacSource([_record(SID, mac_state)], _good_files(SID))
    roots = _roots(tmp_path)

    report = _pull(store, source, roots, make_event_log(tmp_path / "events.jsonl"))

    assert report.pulled == [SID]
    assert state_name(store.get(SID).state) == "received"


def test_pull_registers_unregistered_with_mac_created_at(
    make_store, make_event_log, tmp_path
):
    """VM 未登録セッションは Mac の created_at で register される。"""
    created = datetime(2026, 11, 9, 15, 45, tzinfo=timezone.utc)
    store = make_store()
    source = FakeMacSource(
        [_record(SID, "transfer_pending", created)], _good_files(SID)
    )
    roots = _roots(tmp_path)

    _pull(store, source, roots, make_event_log(tmp_path / "events.jsonl"))

    assert store.get(SID).created_at == created


def test_pull_registers_with_session_id_date_when_created_at_missing(
    make_store, make_event_log, tmp_path
):
    """created_at が無い場合はセッションIDの日付（UTC 0時）で登録する。"""
    store = make_store()
    source = FakeMacSource([_record(SID3, "transfer_pending", None)], _good_files(SID3))
    roots = _roots(tmp_path)

    report = _pull(store, source, roots, make_event_log(tmp_path / "events.jsonl"))

    assert report.pulled == [SID3]
    assert store.get(SID3).created_at == datetime(
        2026, 11, 10, 0, 0, tzinfo=timezone.utc
    )


def test_pull_replaces_staging_on_repull(
    make_store, make_event_log, state_name, tmp_path
):
    """再プル時はステージングを置き換える（古い残骸を持ち越さない）。"""
    store = make_store()
    source = FakeMacSource([_record(SID, "transfer_pending")], _good_files(SID))
    roots = _roots(tmp_path)
    staging_dir = roots[0] / SID
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "system_track.m4a").write_bytes(b"stale-junk")
    (staging_dir / "stale_extra.m4a").write_bytes(b"stale-extra")

    report = _pull(store, source, roots, make_event_log(tmp_path / "events.jsonl"))

    assert report.pulled == [SID]
    assert state_name(store.get(SID).state) == "received"
    # ステージングの古い残骸は置き換えられ、inbox は正しい内容だけ。
    assert not (staging_dir / "stale_extra.m4a").exists()
    assert _inbox_files(roots[1], SID) == {
        "system_track.m4a": SYSTEM_DATA,
        "mic_track.m4a": MIC_DATA,
    }


# ---------------------------------------------------------------------------
# pull_pending: skipped（冪等）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vm_state", ["received", "processing", "final_saved"])
def test_pull_skips_received_or_later_without_fetching(
    make_store,
    drive_to,
    make_event_log,
    state_name,
    t0,
    tmp_path,
    vm_state,
):
    """VM 側で received 以降のセッションは再取得せず skipped に入る。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, vm_state)
    source = FakeMacSource([_record(SID, "transfer_pending")], _good_files(SID))
    roots = _roots(tmp_path)

    report = _pull(store, source, roots, make_event_log(tmp_path / "events.jsonl"))

    assert report.skipped == [SID]
    assert report.pulled == []
    assert report.failed == []
    assert source.fetch_calls == [], "取得済みはフェイク側でも取得しない"
    assert state_name(store.get(SID).state) == vm_state


# ---------------------------------------------------------------------------
# pull_pending: 対象外の Mac 状態
# ---------------------------------------------------------------------------


def test_pull_ignores_non_target_mac_states(make_store, make_event_log, tmp_path):
    """Mac 側が対象外の状態のセッションは対象にせず数えない。"""
    non_targets = [
        "transferring",
        "received",
        "processing",
        "awaiting_editing",
        "draft_saved",
        "final_saved",
        "deletable",
        "closed",
        "unregistered",
    ]
    records = [
        _record(f"2026-11-09_{index:02d}", state)
        for index, state in enumerate(non_targets, start=1)
    ]
    store = make_store()
    source = FakeMacSource(records, {})
    roots = _roots(tmp_path)

    report = _pull(store, source, roots, make_event_log(tmp_path / "events.jsonl"))

    assert report.pulled == []
    assert report.failed == []
    assert report.skipped == []
    assert source.fetch_calls == []
    assert store.all() == [], "対象外は VM へ登録もしない"


# ---------------------------------------------------------------------------
# pull_pending: 取得失敗（transfer_fetch_failed）
# ---------------------------------------------------------------------------


def test_pull_file_fetch_failure_marks_needs_reprocess(
    make_store, make_event_log, state_name, tmp_path
):
    """ファイル取得失敗は transfer_fetch_failed で needs_reprocess。"""
    store = make_store()
    source = FakeMacSource(
        [_record(SID, "transfer_pending")],
        _good_files(SID),
        fail_on={(SID, "mic_track.m4a")},
    )
    roots = _roots(tmp_path)
    log_path = tmp_path / "events.jsonl"

    report = _pull(store, source, roots, make_event_log(log_path))

    assert report.failed == [SID]
    assert report.pulled == []
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_fetch_failed"
    assert _inbox_files(roots[1], SID) == {}
    # 失敗はイベントログに記録される（silent failure 禁止）。
    records = _read_log_records(log_path)
    assert any(
        r.get("session_id") == SID and r.get("error_kind") == "transfer_fetch_failed"
        for r in records
    ), records


def test_pull_fetch_failure_does_not_stop_others(
    make_store, make_event_log, state_name, tmp_path
):
    """1件の取得失敗で他件の処理は止まらない（失敗の隔離）。"""
    store = make_store()
    files = {}
    files.update(_good_files(SID))
    files.update(_good_files(SID2))
    source = FakeMacSource(
        [_record(SID, "transfer_pending"), _record(SID2, "transfer_pending")],
        files,
        fail_on={(SID, "*")},
    )
    roots = _roots(tmp_path)

    report = _pull(store, source, roots, make_event_log(tmp_path / "events.jsonl"))

    assert report.failed == [SID]
    assert report.pulled == [SID2]
    assert state_name(store.get(SID).state) == "needs_reprocess"
    assert state_name(store.get(SID2).state) == "received"


def test_pull_manifest_fetch_failure_is_fetch_failed(
    make_store, make_event_log, state_name, tmp_path
):
    """マニフェスト取得失敗も transfer_fetch_failed で失敗隔離される。"""
    store = make_store()
    source = FakeMacSource(
        [_record(SID, "transfer_pending")],
        _good_files(SID),
        fail_on={(SID, "manifest.json")},
    )
    roots = _roots(tmp_path)
    log_path = tmp_path / "events.jsonl"

    report = _pull(store, source, roots, make_event_log(log_path))

    assert report.failed == [SID]
    assert report.pulled == []
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_fetch_failed"
    records = _read_log_records(log_path)
    assert any(
        r.get("session_id") == SID and r.get("error_kind") == "transfer_fetch_failed"
        for r in records
    )


@pytest.mark.parametrize(
    "raw_manifest",
    [
        b"this-is-not-json",
        b'{"files": []}',  # session_id 欠落
        b'{"session_id": "2026-11-09_01", "files": "oops"}',  # 形式不正
    ],
)
def test_pull_invalid_manifest_is_fetch_failed(
    make_store, make_event_log, state_name, tmp_path, raw_manifest
):
    """マニフェストが不正でも transfer_fetch_failed で失敗隔離される。"""
    store = make_store()
    source = FakeMacSource(
        [_record(SID, "transfer_pending")],
        _good_files(SID),
        manifest_overrides={SID: raw_manifest},
    )
    roots = _roots(tmp_path)

    report = _pull(store, source, roots, make_event_log(tmp_path / "events.jsonl"))

    assert report.failed == [SID]
    assert report.pulled == []
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_fetch_failed"
    assert _inbox_files(roots[1], SID) == {}


def test_pull_list_sessions_failure_is_not_silent(make_store, make_event_log, tmp_path):
    """セッション一覧自体が取得できない場合は黙って成功扱いにしない。"""
    store = make_store()
    source = FakeMacSource([], {}, list_error=_mac_fetch_error("dummy list failure"))
    roots = _roots(tmp_path)

    with pytest.raises(_mac_fetch_error_type()):
        _pull(store, source, roots, make_event_log(tmp_path / "e.jsonl"))


# ---------------------------------------------------------------------------
# pull_pending: 検証失敗（transfer_verification_failed）
# ---------------------------------------------------------------------------


def test_pull_verification_mismatch_is_verification_failed(
    make_store, make_event_log, state_name, tmp_path
):
    """内容がマニフェストと不一致なら transfer_verification_failed。"""
    store = make_store()
    files = _good_files(SID)
    # mic 側だけ、同じ長さの別内容が届く（SHA-256 で検出されるはず）。
    files[(SID, "mic_track.m4a")] = b"X" * len(MIC_DATA)
    source = FakeMacSource([_record(SID, "transfer_pending")], files)
    roots = _roots(tmp_path)
    log_path = tmp_path / "events.jsonl"

    report = _pull(store, source, roots, make_event_log(log_path))

    assert report.failed == [SID]
    assert report.pulled == []
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_verification_failed"
    assert _inbox_files(roots[1], SID) == {}
    records = _read_log_records(log_path)
    assert any(
        r.get("session_id") == SID
        and r.get("error_kind") == "transfer_verification_failed"
        for r in records
    )


# ---------------------------------------------------------------------------
# pull_pending: 再開可能性
# ---------------------------------------------------------------------------


def test_pull_retry_after_failure_reaches_received(
    make_store, make_event_log, state_name, tmp_path
):
    """1回目で取得失敗しても、2回目のプルで received まで進める。"""
    store = make_store()
    roots = _roots(tmp_path)
    event_log = make_event_log(tmp_path / "events.jsonl")

    failing = FakeMacSource(
        [_record(SID, "transfer_pending")],
        _good_files(SID),
        fail_on={(SID, "*")},
    )
    first = _pull(store, failing, roots, event_log)
    assert first.failed == [SID]
    assert state_name(store.get(SID).state) == "needs_reprocess"

    succeeding = FakeMacSource([_record(SID, "transfer_pending")], _good_files(SID))
    second = _pull(store, succeeding, roots, event_log)

    assert second.pulled == [SID]
    assert second.failed == []
    session = store.get(SID)
    assert state_name(session.state) == "received"
    assert _inbox_files(roots[1], SID) == {
        "system_track.m4a": SYSTEM_DATA,
        "mic_track.m4a": MIC_DATA,
    }
    assert len(store.all()) == 1, "再プルでレコードが二重にできない"


def test_pull_resumes_vm_needs_reprocess_transfer_pending(
    make_store, make_event_log, state_name, state_of, tmp_path
):
    """VM 側 needs_reprocess（resume=transfer_pending）を拾い直せる。"""
    store = make_store()
    store.register(SID, CREATED)
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="transfer_fetch_failed",
        resume_state=state_of("transfer_pending"),
    )
    assert state_name(store.get(SID).state) == "needs_reprocess"

    source = FakeMacSource([_record(SID, "transfer_pending")], _good_files(SID))
    roots = _roots(tmp_path)

    report = _pull(store, source, roots, make_event_log(tmp_path / "events.jsonl"))

    assert report.pulled == [SID]
    session = store.get(SID)
    assert state_name(session.state) == "received"
    assert _inbox_files(roots[1], SID) == {
        "system_track.m4a": SYSTEM_DATA,
        "mic_track.m4a": MIC_DATA,
    }


# ---------------------------------------------------------------------------
# SshMacSource（subprocess をモックする）
# ---------------------------------------------------------------------------

SSH_COMMAND = "/dummy/mac-ssh"
REMOTE_APP_DIR = "/dummy/mac-app-data"


def _install_fake_subprocess(monkeypatch, handler):
    """pull モジュールの subprocess 実行をフェイクへ差し替える。

    ``handler(command_text) -> (returncode, stdout_bytes)``。
    呼び出しコマンド（文字列化したもの）の一覧を返す。
    """
    import interview_pipeline.pull as pull_module

    calls: list = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        text = _command_text(command)
        returncode, stdout = handler(text)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")

    if hasattr(pull_module, "subprocess"):
        monkeypatch.setattr(
            pull_module,
            "subprocess",
            SimpleNamespace(run=fake_run),
        )
    if hasattr(pull_module, "run"):
        monkeypatch.setattr(pull_module, "run", fake_run)
    return calls


def _command_text(command) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)


def _ssh_source():
    from interview_pipeline.pull import SshMacSource

    return SshMacSource(SSH_COMMAND, REMOTE_APP_DIR)


def test_ssh_list_sessions_parses_mac_json(monkeypatch):
    """Mac 形式 JSON（sessionId/state/createdAt）を MacSessionRecord へ。"""
    payload = [
        {
            "sessionId": SID,
            "state": "transfer_pending",
            "createdAt": "2026-11-09T09:00:00Z",
            "localPhase": "ready_to_transfer",
            "systemFileName": "system_track.m4a",
            "micFileName": "mic_track.m4a",
        },
        {
            "sessionId": SID2,
            "state": "recorded",
            "createdAt": "2026-11-09T10:30:00+00:00",
            "localPhase": "ready_to_transfer",
            "systemFileName": "system_track.m4a",
            "micFileName": "mic_track.m4a",
        },
    ]
    calls = _install_fake_subprocess(
        monkeypatch,
        lambda text: (0, json.dumps(payload).encode("utf-8")),
    )

    records = _ssh_source().list_sessions()

    assert [r.session_id for r in records] == [SID, SID2]
    assert records[0].state == "transfer_pending"
    assert records[0].created_at == CREATED
    assert records[1].state == "recorded"
    assert records[1].created_at == datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc)
    # リモートパスは引数 remote_app_dir 配下の sessions.json で、
    # SSH コマンド引数経由で読む（実値は引数でのみ与える）。
    assert len(calls) == 1
    text = _command_text(calls[0])
    assert SSH_COMMAND in text
    assert f"{REMOTE_APP_DIR}/sessions.json" in text


def test_ssh_list_sessions_bad_created_at_becomes_none(monkeypatch):
    """createdAt が無い・解釈できないレコードは created_at=None。"""
    payload = [
        {"sessionId": SID, "state": "transfer_pending"},
        {
            "sessionId": SID2,
            "state": "recorded",
            "createdAt": "not-a-date",
        },
    ]
    _install_fake_subprocess(
        monkeypatch,
        lambda text: (0, json.dumps(payload).encode("utf-8")),
    )

    records = _ssh_source().list_sessions()

    assert records[0].created_at is None
    assert records[1].created_at is None


def test_ssh_list_sessions_nonzero_exit_raises_mac_fetch_error(monkeypatch):
    """セッション記録の取得失敗（終了コード非0）は MacFetchError。"""
    _install_fake_subprocess(monkeypatch, lambda text: (255, b""))

    with pytest.raises(_mac_fetch_error_type()):
        _ssh_source().list_sessions()


def test_ssh_fetch_file_returns_stdout_bytes_and_builds_remote_path(
    monkeypatch,
):
    """fetch_file は標準出力を bytes で返し、パスは引数から組み立てる。"""
    calls = _install_fake_subprocess(
        monkeypatch, lambda text: (0, b"\x00\x01dummy-audio")
    )

    data = _ssh_source().fetch_file(SID, "system_track.m4a")

    assert data == b"\x00\x01dummy-audio"
    assert isinstance(data, bytes)
    assert len(calls) == 1
    text = _command_text(calls[0])
    assert SSH_COMMAND in text
    assert f"{REMOTE_APP_DIR}/recordings/{SID}/system_track.m4a" in text


def test_ssh_fetch_file_nonzero_exit_raises_mac_fetch_error(monkeypatch):
    """ファイル取得失敗（終了コード非0）は MacFetchError。"""
    _install_fake_subprocess(monkeypatch, lambda text: (1, b""))

    with pytest.raises(_mac_fetch_error_type()):
        _ssh_source().fetch_file(SID, "system_track.m4a")


# ---------------------------------------------------------------------------
# CLI: pull / _build_mac_source
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """データ領域と Mac 接続情報を一時ディレクトリへ向けた環境。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("INTERVIEW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("INTERVIEW_MAC_SSH_COMMAND", SSH_COMMAND)
    monkeypatch.setenv("INTERVIEW_MAC_APP_DIR", REMOTE_APP_DIR)
    return data_dir


def _install_fake_source(monkeypatch, fake):
    import interview_pipeline.cli as cli

    monkeypatch.setattr(cli, "_build_mac_source", lambda: fake)
    return cli


def test_cli_pull_success_prints_report_lists_and_returns_zero(
    cli_env, monkeypatch, capsys
):
    """pull 成功時は pulled=[...] 形式で表示し、終了コード 0。"""
    fake = FakeMacSource([_record(SID, "transfer_pending")], _good_files(SID))
    cli = _install_fake_source(monkeypatch, fake)

    assert cli.main(["pull"]) == 0

    out = capsys.readouterr().out
    assert f"pulled=['{SID}']" in out
    assert "failed=[]" in out
    assert "skipped=[]" in out
    # VM 側の状態も received まで進んでいること。
    payload = json.loads(
        (cli_env / "state" / "sessions.json").read_text(encoding="utf-8")
    )
    sessions = payload["sessions"] if isinstance(payload, dict) else payload
    state = {s["session_id"]: s["state"] for s in sessions}[SID]
    assert state == "received"


def test_cli_pull_failure_returns_one(cli_env, monkeypatch, capsys):
    """取得失敗がある pull は failed=[...] を表示し、終了コード 1。"""
    fake = FakeMacSource(
        [_record(SID, "transfer_pending")],
        _good_files(SID),
        fail_on={(SID, "*")},
    )
    cli = _install_fake_source(monkeypatch, fake)

    assert cli.main(["pull"]) == 1

    out = capsys.readouterr().out
    assert "pulled=[]" in out
    assert f"failed=['{SID}']" in out
    assert "skipped=[]" in out


def test_cli_pull_skipped_only_returns_zero(cli_env, monkeypatch, capsys):
    """取得済みだけの pull は skipped=[...] を表示し、終了コード 0。"""
    from interview_pipeline.states import State
    from interview_pipeline.store import SessionStore

    store = SessionStore(cli_env / "state" / "sessions.json")
    store.register(SID, CREATED)
    for step in ("transfer_pending", "transferring", "received"):
        store.transition(SID, State(step))

    fake = FakeMacSource([_record(SID, "transfer_pending")], _good_files(SID))
    cli = _install_fake_source(monkeypatch, fake)

    assert cli.main(["pull"]) == 0

    out = capsys.readouterr().out
    assert "pulled=[]" in out
    assert "failed=[]" in out
    assert f"skipped=['{SID}']" in out
    assert fake.fetch_calls == []


def test_build_mac_source_uses_env_values(cli_env):
    """_build_mac_source は環境変数の値で SshMacSource を組み立てる。"""
    from interview_pipeline.cli import _build_mac_source
    from interview_pipeline.pull import SshMacSource

    source = _build_mac_source()

    assert isinstance(source, SshMacSource)
    values = {str(v) for v in vars(source).values()}
    assert SSH_COMMAND in values
    assert REMOTE_APP_DIR in values


def test_build_mac_source_missing_ssh_command_env_raises_system_exit(
    cli_env, monkeypatch
):
    """INTERVIEW_MAC_SSH_COMMAND 未設定なら明確なエラーで終了する。"""
    from interview_pipeline.cli import _build_mac_source

    monkeypatch.delenv("INTERVIEW_MAC_SSH_COMMAND", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        _build_mac_source()
    assert "INTERVIEW_MAC_SSH_COMMAND" in str(excinfo.value.code)


def test_build_mac_source_missing_app_dir_env_raises_system_exit(cli_env, monkeypatch):
    """INTERVIEW_MAC_APP_DIR 未設定なら明確なエラーで終了する。"""
    from interview_pipeline.cli import _build_mac_source

    monkeypatch.delenv("INTERVIEW_MAC_APP_DIR", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        _build_mac_source()
    assert "INTERVIEW_MAC_APP_DIR" in str(excinfo.value.code)


def test_cli_pull_missing_env_raises_system_exit(cli_env, monkeypatch):
    """接続情報が未設定のまま pull を実行しても SystemExit で終わる。"""
    import interview_pipeline.cli as cli

    monkeypatch.delenv("INTERVIEW_MAC_SSH_COMMAND", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["pull"])
