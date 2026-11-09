"""Google連携の設定読み込み。

設定は環境変数 ``INTERVIEW_GOOGLE_CONFIG`` が指す JSON ファイルから
読み込む（実値はリポジトリに置かない。正本 ``docs/状態管理.md``
「データ領域と環境変数」）。欠けたキーは既定値で補う。設定ファイル
自体の欠落・破損は ``GoogleConfigError`` で失敗し、黙って既定値で
進まない（SPEC P2 決定事項 2）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from interview_pipeline.errors import GoogleConfigError

__all__ = ["GoogleConfig", "GoogleConfigError", "load_google_config"]

DEFAULT_CLI_PATH = "/opt/hatch/bin/hatch_gws_cli"
DEFAULT_TIMEZONE = "Asia/Tokyo"
DEFAULT_PRIMARY_KEYWORD = "面接"
DEFAULT_WINDOW_PAST_DAYS = 1
DEFAULT_WINDOW_FUTURE_DAYS = 31


@dataclass
class GoogleConfig:
    """Google連携の設定一式。"""

    cli_path: str = DEFAULT_CLI_PATH
    calendar_ids: list[str] = field(default_factory=lambda: ["primary"])
    timezone: str = DEFAULT_TIMEZONE
    primary_keyword: str = DEFAULT_PRIMARY_KEYWORD
    secondary_keywords: list[str] = field(default_factory=lambda: ["面談"])
    invited_keywords: list[str] = field(
        default_factory=lambda: ["面接", "面談", "インタビュー", "interview"]
    )
    docs_template_id: str | None = None
    docs_folder_id: str | None = None
    window_past_days: int = DEFAULT_WINDOW_PAST_DAYS
    window_future_days: int = DEFAULT_WINDOW_FUTURE_DAYS


_KNOWN_KEYS = {
    "cli_path",
    "calendar_ids",
    "timezone",
    "primary_keyword",
    "secondary_keywords",
    "invited_keywords",
    "docs_template_id",
    "docs_folder_id",
    "window_past_days",
    "window_future_days",
}


def load_google_config(path: Path | str) -> GoogleConfig:
    """JSON ファイルから ``GoogleConfig`` を読み込む。

    未知のキーは無視する（前方互換）。ファイルが無い・JSON が壊れて
    いる場合は ``GoogleConfigError`` を送出する。
    """
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoogleConfigError(f"google config not readable: {config_path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GoogleConfigError(
            f"google config is not valid JSON: {config_path}"
        ) from exc
    if not isinstance(data, dict):
        raise GoogleConfigError(f"google config must be a JSON object: {config_path}")
    values = {key: data[key] for key in _KNOWN_KEYS if key in data}
    return GoogleConfig(**values)
