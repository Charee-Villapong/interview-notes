"""特性テスト（現状固定）: C-058 manifest 検証分岐と store 読込の防御分岐。

根拠: refactoring-candidates-2026-11-07.md の interview-notes
「テストが薄い箇所」（manifest.py from_dict の検証分岐は5件のみで
欠落・型違いの大半が未検証、store.py load の防御分岐は正常な dict
往復のみ）、pipeline/SPEC.md の ``manifest.py`` / ``store.py`` 契約。

本ファイルが固定するのは:
- ``Manifest.from_dict`` の検証分岐（非 dict・キー欠落・型違い）と
  型強制（size の int 化、session_id / name の str 化）、余分なキーの
  無視、``to_dict`` 往復、``Manifest.load`` の失敗伝播
- ``SessionStore.load`` の防御分岐（空ファイル・list 形式・想定外の
  JSON 型・不正 JSON・ファイル無し）
- ``Session.from_dict`` の欠落フィールド既定値と未知状態の拒否
"""

from __future__ import annotations

import json

import pytest

SID = "2026-11-09_01"


# ---------------------------------------------------------------------------
# Manifest.from_dict の検証分岐
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [[], "manifest", 42, None])
def test_manifest_from_dict_rejects_non_dict(bad):
    """dict でない入力は ValueError になる。"""
    from interview_pipeline.manifest import Manifest

    with pytest.raises(ValueError, match="manifest must be a dict"):
        Manifest.from_dict(bad)


def test_manifest_from_dict_rejects_missing_session_id():
    """session_id 欠落は ValueError になる。"""
    from interview_pipeline.manifest import Manifest

    with pytest.raises(ValueError, match="session_id"):
        Manifest.from_dict({"files": []})


def test_manifest_from_dict_rejects_non_list_files():
    """files が list でない場合は ValueError になる。"""
    from interview_pipeline.manifest import Manifest

    with pytest.raises(ValueError, match="must be a list"):
        Manifest.from_dict({"session_id": SID, "files": {"name": "a"}})


def test_manifest_from_dict_rejects_non_dict_entry():
    """ファイルエントリが dict でない場合は ValueError になる。"""
    from interview_pipeline.manifest import Manifest

    with pytest.raises(ValueError, match="must be a dict"):
        Manifest.from_dict({"session_id": SID, "files": ["system_track.m4a"]})


@pytest.mark.parametrize("missing", ["name", "size", "sha256"])
def test_manifest_from_dict_rejects_entry_missing_each_key(missing):
    """エントリの name / size / sha256 の欠落はそれぞれ ValueError。"""
    from interview_pipeline.manifest import Manifest

    entry = {"name": "system_track.m4a", "size": 3, "sha256": "a" * 64}
    del entry[missing]
    with pytest.raises(ValueError, match=missing):
        Manifest.from_dict({"session_id": SID, "files": [entry]})


def test_manifest_from_dict_accepts_empty_files():
    """files が空リストのマニフェストは受理される。"""
    from interview_pipeline.manifest import Manifest

    manifest = Manifest.from_dict({"session_id": SID, "files": []})
    assert manifest.session_id == SID
    assert manifest.files == []


def test_manifest_from_dict_coerces_types():
    """現行挙動: session_id / name は str 化、size は int 化される。"""
    from interview_pipeline.manifest import Manifest

    manifest = Manifest.from_dict(
        {
            "session_id": 20261109,
            "files": [{"name": 123, "size": "42", "sha256": 999}],
        }
    )
    assert manifest.session_id == "20261109"
    entry = manifest.files[0]
    assert entry.name == "123"
    assert entry.size == 42
    assert entry.sha256 == "999"


def test_manifest_from_dict_rejects_non_integer_size_string():
    """整数化できない size 文字列は ValueError になる。"""
    from interview_pipeline.manifest import Manifest

    with pytest.raises(ValueError):
        Manifest.from_dict(
            {
                "session_id": SID,
                "files": [{"name": "a", "size": "4.5", "sha256": "h"}],
            }
        )


def test_manifest_from_dict_ignores_extra_keys():
    """未知のキーは無視される（トップレベル・エントリとも）。"""
    from interview_pipeline.manifest import Manifest

    manifest = Manifest.from_dict(
        {
            "session_id": SID,
            "files": [{"name": "a", "size": 1, "sha256": "h", "extra": True}],
            "unknown": "ignored",
        }
    )
    assert manifest.session_id == SID
    assert len(manifest.files) == 1


def test_manifest_to_dict_round_trip(filekit):
    """to_dict は from_dict の入力と同形の dict を返す。"""
    from interview_pipeline.manifest import Manifest

    data = filekit.manifest_dict(
        SID,
        [("system_track.m4a", b"dummy-system"), ("mic_track.m4a", b"dummy-mic")],
    )
    assert Manifest.from_dict(data).to_dict() == data


def test_manifest_load_invalid_json_raises(tmp_path):
    """壊れた JSON の Manifest.load は JSONDecodeError のまま伝播する。"""
    from interview_pipeline.manifest import Manifest

    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        Manifest.load(path)


def test_manifest_load_missing_file_raises(tmp_path):
    """存在しないファイルの Manifest.load は FileNotFoundError。"""
    from interview_pipeline.manifest import Manifest

    with pytest.raises(FileNotFoundError):
        Manifest.load(tmp_path / "absent.json")


# ---------------------------------------------------------------------------
# SessionStore.load の防御分岐
# ---------------------------------------------------------------------------


def _store_with_content(tmp_path, content, name="sessions.json"):
    from interview_pipeline.store import SessionStore

    path = tmp_path / name
    if content is not None:
        path.write_text(content, encoding="utf-8")
    return SessionStore(path)


def test_store_load_empty_file_yields_no_sessions(tmp_path):
    """空ファイルは例外なく「セッション無し」として読み込まれる。"""
    store = _store_with_content(tmp_path, "")
    assert store.all() == []


def test_store_load_whitespace_only_file_yields_no_sessions(tmp_path):
    """空白だけのファイルも同様に空として扱われる。"""
    store = _store_with_content(tmp_path, "  \n ")
    assert store.all() == []


def test_store_load_accepts_bare_list_payload(tmp_path, t0):
    """sessions キーを持たない list 形式の payload も読み込める。"""
    entry = {
        "session_id": SID,
        "state": "recorded",
        "created_at": t0.isoformat(),
    }
    store = _store_with_content(tmp_path, json.dumps([entry]))
    sessions = store.all()
    assert [s.session_id for s in sessions] == [SID]


@pytest.mark.parametrize("content", ["42", '"hello"', "true", "null"])
def test_store_load_scalar_payload_yields_no_sessions(tmp_path, content):
    """dict でも list でもない JSON は空として扱われる（例外なし）。"""
    store = _store_with_content(tmp_path, content)
    assert store.all() == []


def test_store_load_dict_without_sessions_key_yields_no_sessions(tmp_path):
    """sessions キーの無い dict は空として扱われる。"""
    store = _store_with_content(tmp_path, '{"other": []}')
    assert store.all() == []


def test_store_load_non_list_sessions_raises_type_error(tmp_path):
    """sessions が list でない場合は TypeError になる（現行挙動）。"""
    store_path = tmp_path / "sessions.json"
    store_path.write_text('{"sessions": 42}', encoding="utf-8")
    from interview_pipeline.store import SessionStore

    with pytest.raises(TypeError):
        SessionStore(store_path)


def test_store_load_invalid_json_raises(tmp_path):
    """壊れた JSON は JSONDecodeError のまま伝播する。"""
    store_path = tmp_path / "sessions.json"
    store_path.write_text("{bad", encoding="utf-8")
    from interview_pipeline.store import SessionStore

    with pytest.raises(json.JSONDecodeError):
        SessionStore(store_path)


def test_store_load_missing_file_is_empty_and_load_is_safe(tmp_path):
    """ファイルが無ければ空で始まり、明示 load() を呼んでも例外なし。"""
    from interview_pipeline.store import SessionStore

    store = SessionStore(tmp_path / "absent.json")
    assert store.all() == []
    store.load()
    assert store.all() == []


# ---------------------------------------------------------------------------
# Session.from_dict の既定値
# ---------------------------------------------------------------------------


def test_session_from_dict_defaults_for_missing_optional_fields(t0):
    """任意フィールドが欠けた dict では既定値（None / False）になる。"""
    from interview_pipeline.store import Session

    session = Session.from_dict(
        {
            "session_id": SID,
            "state": "recorded",
            "created_at": t0.isoformat(),
        }
    )
    assert session.resume_state is None
    assert session.error_kind is None
    assert session.draft_saved_at is None
    assert session.final_saved_at is None
    assert session.vm_copy_deleted_at is None
    assert session.mac_copy_deleted_at is None
    assert session.processing_completed is False


def test_session_from_dict_rejects_unknown_state(t0):
    """未知の状態文字列は ValueError になる。"""
    from interview_pipeline.store import Session

    with pytest.raises(ValueError):
        Session.from_dict(
            {
                "session_id": SID,
                "state": "bogus_state",
                "created_at": t0.isoformat(),
            }
        )
