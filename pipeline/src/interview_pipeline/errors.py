"""例外型の集約モジュール。

SPEC 決定事項 7 に基づき、パイプラインで使う例外はすべてここに定義し、
各モジュールはここから再エクスポートする。
"""

from __future__ import annotations


class InvalidTransitionError(Exception):
    """許可されていない状態遷移を行おうとした。"""


class AudioContentError(Exception):
    """保存対象に音声・動画ファイルが含まれていた（保存中止）。"""


class DeletionGuardError(Exception):
    """削除条件を満たしていないため削除を拒否した。"""


class NoteStorageGuardError(Exception):
    """保存先が公開され得ない格納場所であることを確認できなかった（保存中止）。"""


class DraftMarkingError(Exception):
    """自動生成 md の冒頭に「下書き（未照合）」表記がない。"""


class EngineNotConfiguredError(Exception):
    """STT エンジンが未接続・未設定である。"""


class UnclassifiedTrackError(Exception):
    """接頭辞で系統を分類できないファイルがセッション内に存在する。"""


class GoogleGatewayError(Exception):
    """Google へのアクセス（コネクタ経由）が失敗した。"""


class GoogleConfigError(Exception):
    """Google連携の設定が読み込めない・不正である。"""


class MacFetchError(Exception):
    """Mac からの取得（転送プル）が失敗した。"""
