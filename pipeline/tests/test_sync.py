"""検知同期（sync）のテスト。ゲートウェイはテスト側のフェイクで切り分ける。

根拠: pipeline/SPEC.md「P2 追加契約」の ``sync.py`` 契約と
docs/手順書.md「Google連携（P2）／面談検知と行追加」。

- 新規検知 → セッションID採番＋レジストリ登録 → Docsノート作成
  （プレースホルダ置換）→ Sheets へ1行追加、の順に行う。
- 冪等: 再 sync でノート・行を二重に作らない。不足分だけを補う。
- ノート作成失敗は ``note_error`` に種別のみを記録し、成功扱いにしない。
  ノート未作成の間は行追加を保留し、失敗扱いにもしない。
- 行追加失敗は ``sheet_error`` に記録し、次回は行だけを再試行する。
- 1件の失敗で他件の処理は止めない（silent failure 禁止）。
- 未録音の予定を SessionStore へ登録しない。
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=JST)
START = datetime(2026, 11, 10, 10, 0, tzinfo=JST)
END = datetime(2026, 11, 10, 11, 0, tzinfo=JST)
SID = "2026-11-10_01"
SID2 = "2026-11-10_02"

_ERROR_SEARCH_MODULES = (
    "interview_pipeline.gateway",
    "interview_pipeline.errors",
    "interview_pipeline.sync",
    "interview_pipeline",
)


def _gateway_error(message="dummy gateway failure"):
    """フェイクが送出するゲートウェイ例外を作る。"""
    for module_name in _ERROR_SEARCH_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        cls = getattr(module, "GoogleGatewayError", None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            return cls(message)
    return RuntimeError(message)


class FakeGateway:
    """テスト側で定義する GoogleGateway のフェイク実装（実接続しない）。"""

    def __init__(self, events_by_calendar=None):
        self.events_by_calendar = events_by_calendar or {}
        self.copy_calls: list[str] = []  # copy_template に渡されたノート名
        self.created_docs: dict[str, str] = {}  # doc_id -> URL
        self.replace_calls: list[tuple[str, dict]] = []
        self.append_calls: list[list[str]] = []  # 追加に成功した行
        self.append_attempts = 0
        self.note_texts: dict[str, str] = {}
        # 失敗制御: ノート名に含まれる場合に copy を失敗させる部分文字列。
        self.fail_copy_for: set[str] = set()
        self.fail_append = False
        self._seq = 0

    def list_events(self, calendar_id, time_min, time_max):
        return list(self.events_by_calendar.get(calendar_id, []))

    def copy_template(self, template_id, name, folder_id=None):
        self.copy_calls.append(name)
        if any(marker in name for marker in self.fail_copy_for):
            raise _gateway_error("dummy note creation failure")
        self._seq += 1
        doc_id = f"dummy-doc-{self._seq}"
        url = f"https://example.com/docs/{doc_id}"
        self.created_docs[doc_id] = url
        return doc_id, url

    def replace_note_fields(self, doc_id, fields):
        self.replace_calls.append((doc_id, dict(fields)))

    def read_note_text(self, doc_id):
        return self.note_texts.get(doc_id, "")

    def append_sheet_row(self, spreadsheet_id, tab, values):
        self.append_attempts += 1
        if self.fail_append:
            raise _gateway_error("dummy sheet append failure")
        self.append_calls.append(list(values))
        # 行番号は 1 始まりの連番として返す（見出し行の有無は見ない）。
        return len(self.append_calls) + 1

    def sheet_row_exists(self, spreadsheet_id, tab, session_id):
        return any(
            session_id in str(value) for row in self.append_calls for value in row
        )


def _config(tmp_path, **overrides):
    from interview_pipeline.google_config import load_google_config

    data = {
        "cli_path": "/dummy/path/hatch_gws_cli",
        "calendar_ids": ["dummy-calendar-1"],
        "timezone": "Asia/Tokyo",
        "primary_keyword": "面接",
        "secondary_keywords": ["面談"],
        "invited_keywords": ["面接", "面談"],
        "sheets_spreadsheet_id": "dummy-spreadsheet-id",
        "sheets_tab": "面談一覧",
        "docs_template_id": "dummy-template-id",
        "window_past_days": 7,
        "window_future_days": 30,
    }
    data.update(overrides)
    path = tmp_path / "google_config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return load_google_config(path)


def _event(**overrides):
    from interview_pipeline.gateway import CalendarEvent

    base = {
        "event_id": "evt-dummy-1",
        "calendar_id": "dummy-calendar-1",
        "title": "面接（ダミー社・担当者名）",
        "start": START,
        "end": END,
        "organizer_email": "organizer@example.com",
        "organizer_self": True,
        "location": "",
        "description": "",
        "meeting_url": "https://meet.google.com/dummy-example",
        "cancelled": False,
    }
    base.update(overrides)
    return CalendarEvent(**base)


def _setup(tmp_path, make_store, events):
    from interview_pipeline.interviews import InterviewRegistry

    registry = InterviewRegistry(tmp_path / "interviews.json")
    gateway = FakeGateway({"dummy-calendar-1": events})
    store = make_store()
    return registry, gateway, store


def _sync(registry, gateway, config, store):
    from interview_pipeline.sync import sync_interviews

    return sync_interviews(registry, gateway, config, store=store, now=NOW)


def test_sync_new_event_registers_note_and_row(tmp_path, make_store):
    """新規検知でレコード登録＋ノート作成＋行追加まで行われる。"""
    registry, gateway, store = _setup(tmp_path, make_store, [_event()])
    report = _sync(registry, gateway, _config(tmp_path), store)

    assert report.detected == [SID]
    assert report.notes_created == [SID]
    assert report.rows_added == [SID]
    assert report.failed == []

    record = registry.get_by_event("evt-dummy-1")
    assert record.session_id == SID
    assert record.date == "2026-11-10"
    assert record.company == "ダミー社"
    assert record.docs_doc_id in gateway.created_docs
    assert record.docs_url == gateway.created_docs[record.docs_doc_id]
    assert record.sheet_row is not None
    assert record.note_error is None
    assert record.sheet_error is None

    # ノート名は「<session_id> 面談ノート」（企業名・個人名を入れない）。
    assert gateway.copy_calls == [f"{SID} 面談ノート"]
    # 基本情報プレースホルダが置換される。
    assert len(gateway.replace_calls) == 1
    fields = gateway.replace_calls[0][1]
    assert {"日時", "企業", "形式", "会議URL"} <= set(fields)
    assert fields["企業"] == "ダミー社"
    assert fields["会議URL"] == "https://meet.google.com/dummy-example"
    assert "Google Meet" in fields["形式"]

    # 行は 列: 日付・企業・形式・結果・次のアクション・Docsリンク の6列。
    assert len(gateway.append_calls) == 1
    row = gateway.append_calls[0]
    assert len(row) == 6
    assert row[0] == "2026-11-10"
    assert row[1] == "ダミー社"
    assert row[5] == record.docs_url

    # 未録音の予定を SessionStore へは登録しない。
    assert store.all() == []


def test_sync_second_run_creates_nothing_twice(tmp_path, make_store):
    """2回目の sync では二重作成せず、レポートは空になる。"""
    registry, gateway, store = _setup(tmp_path, make_store, [_event()])
    config = _config(tmp_path)
    _sync(registry, gateway, config, store)

    report = _sync(registry, gateway, config, store)

    assert report.detected == []
    assert report.notes_created == []
    assert report.rows_added == []
    assert report.failed == []
    assert len(gateway.copy_calls) == 1
    assert len(gateway.append_calls) == 1
    assert len(registry.all()) == 1


def test_sync_note_failure_records_error_and_defers_row(tmp_path, make_store):
    """ノート作成失敗: note_error に種別が残り、行は追加されず失敗扱いにしない。

    次回成功時はノートだけを作り直し、行も追加される。
    """
    registry, gateway, store = _setup(tmp_path, make_store, [_event()])
    config = _config(tmp_path)
    gateway.fail_copy_for = {SID}

    report = _sync(registry, gateway, config, store)

    record = registry.get_by_event("evt-dummy-1")
    assert record.session_id == SID
    assert isinstance(record.note_error, str) and record.note_error
    assert not record.docs_doc_id
    assert record.sheet_row is None
    assert gateway.append_calls == []
    assert SID not in report.failed
    assert report.notes_created == []
    assert report.rows_added == []

    # 次回（失敗要因が解消された状態）ではノートを作り直して行も追加する。
    gateway.fail_copy_for = set()
    report2 = _sync(registry, gateway, config, store)
    record = registry.get_by_event("evt-dummy-1")
    assert report2.notes_created == [SID]
    assert report2.rows_added == [SID]
    assert len(gateway.created_docs) == 1
    assert record.docs_doc_id in gateway.created_docs
    assert record.sheet_row is not None
    assert len(gateway.append_calls) == 1


def test_sync_sheet_failure_records_error_and_retries_row_only(tmp_path, make_store):
    """行追加失敗: sheet_error が記録され、次回は行だけ再試行する。

    ノートは作り直さない（作成済みノートは1件のまま）。
    """
    registry, gateway, store = _setup(tmp_path, make_store, [_event()])
    config = _config(tmp_path)
    gateway.fail_append = True

    _sync(registry, gateway, config, store)

    record = registry.get_by_event("evt-dummy-1")
    assert isinstance(record.sheet_error, str) and record.sheet_error
    assert record.sheet_row is None
    assert record.docs_doc_id in gateway.created_docs
    assert gateway.append_calls == []

    gateway.fail_append = False
    report2 = _sync(registry, gateway, config, store)

    record = registry.get_by_event("evt-dummy-1")
    assert report2.rows_added == [SID]
    assert report2.notes_created == []
    assert len(gateway.copy_calls) == 1  # ノートは作り直していない
    assert len(gateway.created_docs) == 1
    assert record.sheet_row is not None
    assert len(gateway.append_calls) == 1


def test_sync_one_failure_does_not_stop_others(tmp_path, make_store):
    """1件のノート作成が失敗しても、他件は最後まで処理される。"""
    events = [
        _event(event_id="evt-dummy-1"),
        _event(event_id="evt-dummy-2", title="面接（第2ダミー社・担当者名）"),
    ]
    registry, gateway, store = _setup(tmp_path, make_store, events)
    gateway.fail_copy_for = {SID}  # 1件目（_01）のノート作成だけ失敗させる

    report = _sync(registry, gateway, _config(tmp_path), store)

    assert set(report.detected) == {SID, SID2}
    assert report.notes_created == [SID2]
    assert report.rows_added == [SID2]

    failed_record = registry.get_by_event("evt-dummy-1")
    assert failed_record.note_error
    assert failed_record.sheet_row is None

    ok_record = registry.get_by_event("evt-dummy-2")
    assert ok_record.company == "第2ダミー社"
    assert ok_record.note_error is None
    assert ok_record.sheet_row is not None
    assert len(gateway.append_calls) == 1


def test_sync_ignores_non_interview_and_cancelled_events(tmp_path, make_store):
    """検知対象外・キャンセル済みの予定はレコードを作らない。"""
    events = [
        _event(event_id="evt-dummy-plain", title="定例会議"),
        _event(event_id="evt-dummy-cancelled", cancelled=True),
    ]
    registry, gateway, store = _setup(tmp_path, make_store, events)

    report = _sync(registry, gateway, _config(tmp_path), store)

    assert report.detected == []
    assert report.notes_created == []
    assert report.rows_added == []
    assert registry.all() == []
    assert gateway.copy_calls == []
    assert gateway.append_calls == []
