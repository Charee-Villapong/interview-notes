"""データ領域のレイアウト定数。

ファイル名とデータ領域配下のサブパスは SPEC で固定された配置規約。
変更箇所が複数モジュールに跨らないよう、リテラルをここに集約する。
値そのものは契約なので変更しない。
"""

from __future__ import annotations

__all__ = [
    "EVENTS_FILENAME",
    "INBOX_DIRNAME",
    "LOG_DIRNAME",
    "MANIFEST_FILENAME",
    "SESSIONS_FILENAME",
    "STAGING_DIRNAME",
    "STATE_DIRNAME",
    "TRANSCRIPT_FILENAME",
    "WORK_DIRNAME",
]

# ファイル名
MANIFEST_FILENAME = "manifest.json"
TRANSCRIPT_FILENAME = "transcript.md"
SESSIONS_FILENAME = "sessions.json"
EVENTS_FILENAME = "events.jsonl"

# データ領域直下のサブディレクトリ名
STAGING_DIRNAME = "staging"
INBOX_DIRNAME = "inbox"
WORK_DIRNAME = "work"
STATE_DIRNAME = "state"
LOG_DIRNAME = "log"
