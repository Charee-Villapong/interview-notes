"""Google連携設定（google_config）のテスト。

根拠: pipeline/SPEC.md「P2 追加契約」の ``google_config.py`` 契約と
docs/手順書.md「Google連携（P2）／設定」。

- 設定は環境変数 ``INTERVIEW_GOOGLE_CONFIG`` が指す JSON ファイルから
  読み込む（実値はリポジトリに置かない）。
- 欠けたキーは既定値（キーワード類・ウィンドウ・タイムゾーン）で補う。
  主キーの既定は「面接」、副キーは「面談」を含み既定で有効
  （docs/手順書.md、docs/開発ノート.md の P2 論点）。
- テストデータはすべてダミー値を使う。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

#: フル指定のダミー設定（実在の ID・パスは使わない）。
FULL_CONFIG = {
    "cli_path": "/dummy/path/hatch_gws_cli",
    "calendar_ids": ["dummy-calendar-1", "dummy-calendar-2"],
    "timezone": "Asia/Tokyo",
    "primary_keyword": "面接",
    "secondary_keywords": ["面談"],
    "invited_keywords": ["面接", "面談"],
    "docs_template_id": "dummy-template-id",
    "docs_folder_id": "dummy-folder-id",
    "window_past_days": 7,
    "window_future_days": 30,
}


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "google_config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _load(tmp_path: Path, data: dict):
    from interview_pipeline.google_config import load_google_config

    return load_google_config(_write_config(tmp_path, data))


def test_load_google_config_reads_all_keys(tmp_path):
    """JSON の全キーが対応する属性として読み込まれる。"""
    config = _load(tmp_path, FULL_CONFIG)

    assert config.cli_path == FULL_CONFIG["cli_path"]
    assert list(config.calendar_ids) == FULL_CONFIG["calendar_ids"]
    assert config.timezone == "Asia/Tokyo"
    assert config.primary_keyword == "面接"
    assert list(config.secondary_keywords) == ["面談"]
    assert list(config.invited_keywords) == ["面接", "面談"]
    assert config.docs_template_id == "dummy-template-id"
    assert config.docs_folder_id == "dummy-folder-id"
    assert config.window_past_days == 7
    assert config.window_future_days == 30


def test_load_google_config_completes_defaults(tmp_path):
    """欠けたキー（キーワード・ウィンドウ・タイムゾーン）は既定値で補われる。

    既定値のうち仕様・手順書で確定しているもの:
    主キー「面接」、副キーに「面談」を含む、タイムゾーン「Asia/Tokyo」。
    ウィンドウの日数の具体値は仕様に無いため、整数で補われることだけを見る。
    """
    minimal = {
        "docs_template_id": "dummy-template-id",
    }
    config = _load(tmp_path, minimal)

    assert config.primary_keyword == "面接"
    assert "面談" in list(config.secondary_keywords)
    # 招待キーワードも既定値を持つ（docs/開発ノート.md: 変更可能な既定値）。
    assert len(list(config.invited_keywords)) >= 1
    assert config.timezone == "Asia/Tokyo"
    assert isinstance(config.window_past_days, int)
    assert config.window_past_days >= 0
    assert isinstance(config.window_future_days, int)
    assert config.window_future_days >= 0
    # 検知対象カレンダーはリストとして補われる（中身は設定で与える前提）。
    assert isinstance(list(config.calendar_ids), list)


def test_load_google_config_explicit_values_override_defaults(tmp_path):
    """明示した値は既定値より優先される。"""
    data = dict(FULL_CONFIG)
    data.update(
        {
            "primary_keyword": "ダミー主キー",
            "secondary_keywords": ["ダミー副キー"],
            "invited_keywords": ["ダミー招待キー"],
            "timezone": "UTC",
            "window_past_days": 1,
            "window_future_days": 2,
        }
    )
    config = _load(tmp_path, data)

    assert config.primary_keyword == "ダミー主キー"
    assert list(config.secondary_keywords) == ["ダミー副キー"]
    assert list(config.invited_keywords) == ["ダミー招待キー"]
    assert config.timezone == "UTC"
    assert config.window_past_days == 1
    assert config.window_future_days == 2


def test_load_google_config_docs_folder_id_is_optional(tmp_path):
    """任意キーの docs_folder_id は無くても読み込め、空として扱われる。"""
    data = dict(FULL_CONFIG)
    del data["docs_folder_id"]
    config = _load(tmp_path, data)

    assert not config.docs_folder_id


def test_load_google_config_missing_file_fails_clearly(tmp_path):
    """設定ファイルが存在しない場合は、明確に失敗する（既定で進めない）。"""
    from interview_pipeline.google_config import load_google_config

    with pytest.raises(Exception):
        load_google_config(tmp_path / "does-not-exist.json")


def test_load_google_config_invalid_json_fails(tmp_path):
    """壊れた JSON は失敗として扱われ、黙って既定値で進まない。"""
    from interview_pipeline.google_config import load_google_config

    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(Exception):
        load_google_config(path)
