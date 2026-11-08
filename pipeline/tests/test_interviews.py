"""検知レジストリ・検知判定・抽出（interviews）のテスト。

根拠: pipeline/SPEC.md「P2 追加契約」の ``interviews.py`` 契約、
docs/状態管理.md「セッション識別子」（採番規則 ``<日付>_NN``）、
docs/手順書.md「Google連携（P2）」。

- レジストリは ``SessionStore`` とは別の永続記録で、``event_id`` で冪等。
- セッションIDの連番は、SessionStore の既存セッションとレジストリ内の
  同日レコードの両方を見て決める。
- 企業名・形式の抽出はベストエフォートで、取れなければ空文字列
  （推測で埋めない）。
- テストデータはすべてダミー。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
CREATED = datetime(2026, 11, 8, 9, 0, tzinfo=JST)
DAY = "2026-11-10"
START = datetime(2026, 11, 10, 10, 0, tzinfo=JST)
END = datetime(2026, 11, 10, 11, 0, tzinfo=JST)


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
        "meeting_url": "",
        "cancelled": False,
    }
    base.update(overrides)
    return CalendarEvent(**base)


def _record(**overrides):
    from interview_pipeline.interviews import InterviewRecord

    base = {
        "event_id": "evt-dummy-1",
        "session_id": "2026-11-10_01",
        "date": DAY,
        "start": START,
        "end": END,
        "title": "面接（ダミー社・担当者名）",
        "company": "ダミー社",
        "format": "Google Meet",
        "meeting_url": "https://meet.google.com/dummy-example",
        "docs_doc_id": "dummy-doc-1",
        "docs_url": "https://example.com/docs/dummy-doc-1",
        "sheet_row": 1,
        "note_error": None,
        "sheet_error": None,
        "reminders": {},
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    base.update(overrides)
    return InterviewRecord(**base)


def _registry(tmp_path, name="interviews.json"):
    from interview_pipeline.interviews import InterviewRegistry

    return InterviewRegistry(tmp_path / name)


# ---------------------------------------------------------------------------
# InterviewRegistry: upsert の冪等性と永続化
# ---------------------------------------------------------------------------


def test_registry_upsert_registers_record(tmp_path):
    """upsert で登録でき、event_id / session_id の両方から引ける。"""
    registry = _registry(tmp_path)
    record = _record()
    returned = registry.upsert(record)

    assert returned.event_id == "evt-dummy-1"
    assert registry.get_by_event("evt-dummy-1").session_id == "2026-11-10_01"
    assert registry.get("2026-11-10_01").event_id == "evt-dummy-1"
    assert [r.event_id for r in registry.all()] == ["evt-dummy-1"]


def test_registry_upsert_is_idempotent_by_event_id(tmp_path):
    """同一 event_id の upsert は冪等で、既存レコードが返り二重に作られない。"""
    registry = _registry(tmp_path)
    first = registry.upsert(_record())
    # 2回目は別タイトル・別セッションIDで渡しても既存が返る。
    second = registry.upsert(
        _record(session_id="2026-11-10_99", title="面接（別ダミー社）")
    )

    assert len(registry.all()) == 1
    assert second.event_id == first.event_id
    assert second.session_id == first.session_id
    assert second.title == first.title


def test_registry_persists_across_reload(tmp_path):
    """レジストリは永続化され、再インスタンス化で復元される。"""
    registry = _registry(tmp_path, "persist.json")
    registry.upsert(
        _record(
            sheet_row=3,
            note_error=None,
            reminders={"eve": "2026-11-09T09:00:00+09:00"},
        )
    )
    registry.save()

    reloaded = _registry(tmp_path, "persist.json")
    if not reloaded.all():
        # コンストラクタで読み込まない実装の場合は明示的に load する。
        load = getattr(reloaded, "load", None)
        if callable(load):
            load()

    assert len(reloaded.all()) == 1
    record = reloaded.get_by_event("evt-dummy-1")
    assert record.session_id == "2026-11-10_01"
    assert record.date == DAY
    assert record.company == "ダミー社"
    assert record.sheet_row == 3
    assert record.note_error is None
    assert record.reminders == {"eve": "2026-11-09T09:00:00+09:00"}


# ---------------------------------------------------------------------------
# InterviewRegistry.assign_session_id（採番規則 <日付>_NN）
# ---------------------------------------------------------------------------


def test_assign_session_id_first_of_day(tmp_path, make_store):
    """同日セッションがどこにも無ければ ``_01`` が採番される。"""
    registry = _registry(tmp_path)
    store = make_store()

    assert registry.assign_session_id(DAY, store) == "2026-11-10_01"


def test_assign_session_id_avoids_session_store(tmp_path, make_store, t0):
    """SessionStore に同日の既存セッションがあれば次の連番になる。"""
    registry = _registry(tmp_path)
    store = make_store()
    store.register("2026-11-10_01", t0)

    assert registry.assign_session_id(DAY, store) == "2026-11-10_02"


def test_assign_session_id_avoids_registry_records(tmp_path, make_store):
    """レジストリ内に同日レコードがあれば次の連番になる。"""
    registry = _registry(tmp_path)
    registry.upsert(_record())
    store = make_store()

    assert registry.assign_session_id(DAY, store) == "2026-11-10_02"


def test_assign_session_id_avoids_both_sources(tmp_path, make_store, t0):
    """ストアとレジストリの両方と衝突しない最小の連番が返る。"""
    registry = _registry(tmp_path)
    registry.upsert(_record(session_id="2026-11-10_02"))
    store = make_store()
    store.register("2026-11-10_01", t0)

    assert registry.assign_session_id(DAY, store) == "2026-11-10_03"


def test_assign_session_id_other_date_unaffected(tmp_path, make_store, t0):
    """別日の既存セッションは採番に影響しない。"""
    registry = _registry(tmp_path)
    registry.upsert(_record())
    store = make_store()
    store.register("2026-11-10_01", t0)

    assert registry.assign_session_id("2026-11-11", store) == "2026-11-11_01"


# ---------------------------------------------------------------------------
# is_interview_event（検知判定）
# ---------------------------------------------------------------------------


def test_is_interview_event_cancelled_is_none(tmp_path):
    """キャンセル済み予定は、キーワードがあっても常に対象外。"""
    from interview_pipeline.interviews import is_interview_event

    event = _event(cancelled=True)
    assert is_interview_event(event, _config(tmp_path)) is None


def test_is_interview_event_self_primary(tmp_path):
    """自己登録＋タイトルに主キー → "primary"。"""
    from interview_pipeline.interviews import is_interview_event

    event = _event(title="面接（ダミー社・担当者名）", organizer_self=True)
    assert is_interview_event(event, _config(tmp_path)) == "primary"


def test_is_interview_event_self_secondary(tmp_path):
    """自己登録＋タイトルに副キーのみ → "secondary"。"""
    from interview_pipeline.interviews import is_interview_event

    event = _event(title="面談（ダミー社・担当者名）", organizer_self=True)
    assert is_interview_event(event, _config(tmp_path)) == "secondary"


def test_is_interview_event_self_without_keyword_is_none(tmp_path):
    """自己登録でもキーワードが無ければ対象外。"""
    from interview_pipeline.interviews import is_interview_event

    event = _event(title="定例会議", organizer_self=True)
    assert is_interview_event(event, _config(tmp_path)) is None


def test_is_interview_event_invited_by_title(tmp_path):
    """招待予定＋タイトルに招待キーワード → "invited"。"""
    from interview_pipeline.interviews import is_interview_event

    event = _event(
        title="面接のご案内（ダミー社）",
        organizer_self=False,
        organizer_email="recruiter@example.com",
    )
    assert is_interview_event(event, _config(tmp_path)) == "invited"


def test_is_interview_event_invited_by_description(tmp_path):
    """招待予定＋説明文に招待キーワード → "invited"。"""
    from interview_pipeline.interviews import is_interview_event

    event = _event(
        title="日程調整のお願い",
        description="ダミー社との面談の日程についてご相談です。",
        organizer_self=False,
        organizer_email="recruiter@example.com",
    )
    assert is_interview_event(event, _config(tmp_path)) == "invited"


def test_is_interview_event_invited_without_keyword_is_none(tmp_path):
    """招待予定でもタイトル・説明文にキーワードが無ければ対象外。"""
    from interview_pipeline.interviews import is_interview_event

    event = _event(
        title="説明会のご案内",
        description="よろしくお願いいたします。",
        organizer_self=False,
        organizer_email="recruiter@example.com",
    )
    assert is_interview_event(event, _config(tmp_path)) is None


# ---------------------------------------------------------------------------
# extract_company（企業名候補の抽出）
# ---------------------------------------------------------------------------


def test_extract_company_fullwidth_parentheses_first_element():
    """全角括弧「（ダミー社・担当者名）」からは先頭要素を取る。"""
    from interview_pipeline.interviews import extract_company

    event = _event(title="面接（ダミー社・担当者名）")
    assert extract_company(event) == "ダミー社"


def test_extract_company_halfwidth_parentheses():
    """半角括弧でも括弧内の先頭要素を取る。"""
    from interview_pipeline.interviews import extract_company

    event = _event(title="面接(ダミー社・担当者名)")
    assert extract_company(event) == "ダミー社"


def test_extract_company_single_element_parentheses():
    """括弧内が1要素だけならその要素を取る。"""
    from interview_pipeline.interviews import extract_company

    event = _event(title="面接（ダミー社）")
    assert extract_company(event) == "ダミー社"


def test_extract_company_without_parentheses_is_empty():
    """括弧が無ければ推測で埋めず、空文字列を返す。"""
    from interview_pipeline.interviews import extract_company

    event = _event(title="面接")
    assert extract_company(event) == ""


# ---------------------------------------------------------------------------
# extract_format（形式の抽出）
# ---------------------------------------------------------------------------


def test_extract_format_uses_location():
    """場所があれば場所を形式に使う。"""
    from interview_pipeline.interviews import extract_format

    event = _event(location="ダミー会議室")
    assert "ダミー会議室" in extract_format(event)


def test_extract_format_meet_url_in_description():
    """説明文に Meet の URL があれば Google Meet を含む形式になる。"""
    from interview_pipeline.interviews import extract_format

    event = _event(
        description="参加はこちら: https://meet.google.com/dummy-example",
    )
    assert "Google Meet" in extract_format(event)


def test_extract_format_meet_url_in_meeting_url():
    """会議URLに Meet の URL があれば Google Meet を含む形式になる。"""
    from interview_pipeline.interviews import extract_format

    event = _event(meeting_url="https://meet.google.com/dummy-example")
    assert "Google Meet" in extract_format(event)


def test_extract_format_zoom_url_in_description():
    """説明文に Zoom の URL があれば Zoom を含む形式になる。"""
    from interview_pipeline.interviews import extract_format

    event = _event(description="参加はこちら: https://zoom.us/j/dummy-example")
    assert "Zoom" in extract_format(event)


def test_extract_format_without_clues_is_empty():
    """場所も会議URLも無ければ空文字列（推測で埋めない）。"""
    from interview_pipeline.interviews import extract_format

    event = _event(location="", description="", meeting_url="")
    assert extract_format(event) == ""
