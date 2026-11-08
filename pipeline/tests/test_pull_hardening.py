"""プル転送の追加担保（QA推奨対応で実装係が追加したテスト）。

根拠: pipeline/SPEC.md「PULL 追加契約」— セッションIDが正本の形式
（``YYYY-MM-DD_NN``）に合わないレコードは、登録も取得もせず、
イベントを記録して ``failed`` に入れる（ステージングや録音置き場の
外を触らないための担保。MacSource の実装に依らない）。
"""

from __future__ import annotations

from pathlib import Path


class _FakeSource:
    """セッション一覧だけを返し、取得が呼ばれたら記録するフェイク。"""

    def __init__(self, records):
        self._records = records
        self.fetched: list[tuple[str, str]] = []

    def list_sessions(self):
        return list(self._records)

    def fetch_file(self, session_id, name):
        self.fetched.append((session_id, name))
        raise AssertionError("fetch_file must not be called")


class _FakeEventLog:
    def __init__(self):
        self.entries: list[tuple[str, str, str | None]] = []

    def record(self, session_id, state, error_kind=None):
        name = getattr(state, "value", str(state))
        self.entries.append((session_id, name, error_kind))


def test_malformed_session_id_is_failed_without_register_or_fetch(
    make_store, tmp_path: Path
):
    """形式外IDは登録も取得もせず、イベント付きで failed に入る。"""
    from interview_pipeline.pull import MacSessionRecord, pull_pending

    store = make_store()
    event_log = _FakeEventLog()
    source = _FakeSource(
        [MacSessionRecord(session_id="../evil", state="transfer_pending")]
    )

    report = pull_pending(
        store,
        source,
        staging_root=tmp_path / "staging",
        inbox_root=tmp_path / "inbox",
        event_log=event_log,
    )

    assert report.failed == ["../evil"]
    assert report.pulled == [] and report.skipped == []
    assert source.fetched == []
    assert store.all() == []
    assert event_log.entries == [
        ("../evil", "needs_reprocess", "transfer_fetch_failed")
    ]
    # ステージング側に何も作られていないこと。
    assert not (tmp_path / "staging").exists()


def test_session_id_with_vm_suffix_is_accepted_shape():
    """形式判定は ``-2`` サフィックス形式も受理する（採番規則の差異）。"""
    from interview_pipeline.pull import _SESSION_ID_PATTERN

    assert _SESSION_ID_PATTERN.match("2026-11-09_01")
    assert _SESSION_ID_PATTERN.match("2026-11-09_01-2")
    assert not _SESSION_ID_PATTERN.match("2026-11-09")
    assert not _SESSION_ID_PATTERN.match("recorded_01")
