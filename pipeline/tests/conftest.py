"""テスト共通のフィクスチャとフェイク。

設計方針:
- ``interview_pipeline`` への import は、フィクスチャが返すファクトリ関数や
  フェイクのメソッドの中（＝テスト本体の実行時）でのみ行う。トップレベルで
  import すると、未実装の現状ではコレクションエラーで全滅してしまうため。
  これにより各テストが「未実装」を理由に個別に失敗する。
- 個人名・メール・IP等の実値は使わない。セッションIDは日付ベースのダミー、
  音声ファイルはダミーバイト列で代用する。
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

#: 基準時刻（録音日時ダミー）。日付はセッションIDと揃える。
T0 = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)

#: 状態名（docs/状態管理.md の状態一覧）から、その状態へ到達するための
#: 正規の遷移経路。テストのセットアップでセッションを任意の状態まで進める。
_DRIVE_PATHS: dict[str, list[str]] = {
    "recorded": [],
    "transfer_pending": ["transfer_pending"],
    "transferring": ["transfer_pending", "transferring"],
    "received": ["transfer_pending", "transferring", "received"],
    "processing": ["transfer_pending", "transferring", "received", "processing"],
    "awaiting_editing": [
        "transfer_pending",
        "transferring",
        "received",
        "processing",
        "awaiting_editing",
    ],
    "draft_saved": [
        "transfer_pending",
        "transferring",
        "received",
        "processing",
        "awaiting_editing",
        "draft_saved",
    ],
    "final_saved": [
        "transfer_pending",
        "transferring",
        "received",
        "processing",
        "awaiting_editing",
        "draft_saved",
        "final_saved",
    ],
    "deletable": [
        "transfer_pending",
        "transferring",
        "received",
        "processing",
        "awaiting_editing",
        "draft_saved",
        "final_saved",
        "deletable",
    ],
    "closed": [
        "transfer_pending",
        "transferring",
        "received",
        "processing",
        "awaiting_editing",
        "draft_saved",
        "final_saved",
        "deletable",
        "closed",
    ],
    # 任意の状態から needs_reprocess へは遷移できる（状態管理.md）。
    "needs_reprocess": ["needs_reprocess"],
}

#: 例外型を探す候補モジュール（SPEC が定義モジュールを明記していないため）。
_EXCEPTION_SEARCH_MODULES = (
    "interview_pipeline.states",
    "interview_pipeline.store",
    "interview_pipeline.receive",
    "interview_pipeline.guards",
    "interview_pipeline.publish",
    "interview_pipeline.transcribe",
    "interview_pipeline.runner",
    "interview_pipeline.saving",
    "interview_pipeline.manifest",
    "interview_pipeline.errors",
    "interview_pipeline.exceptions",
    "interview_pipeline",
)


@pytest.fixture
def t0() -> datetime:
    """基準となる録音日時（2026-11-09 09:00 UTC）。"""
    return T0


@pytest.fixture
def state_of():
    """状態名（例: "received"）から State メンバを引く関数を返す。

    メンバ名の大文字小文字や値の持ち方に依存しないよう、名前・値の
    どちらが状態一覧の表記と一致しても引けるようにする。
    """

    def _state_of(name: str):
        from interview_pipeline.states import State

        for member in State:
            value = member.value if isinstance(member.value, str) else None
            if value == name or member.name == name or member.name.lower() == name:
                return member
        raise AssertionError(f"State に {name!r} という状態がありません")

    return _state_of


@pytest.fixture
def state_name():
    """State メンバ（または文字列）から状態名文字列へ正規化する関数を返す。"""

    def _state_name(state) -> str:
        if state is None:
            return ""
        if isinstance(state, str):
            return state
        value = getattr(state, "value", None)
        if isinstance(value, str):
            return value
        return str(getattr(state, "name", state)).lower()

    return _state_name


@pytest.fixture
def exc():
    """例外型の名前からクラスを探す関数を返す。

    SPEC は例外名を示すが定義モジュールを一意に定めていないものがあるため、
    パッケージ内の候補モジュールを横断して探す。見つからなければ
    AssertionError でテストを失敗させる。
    """

    def _exc(name: str):
        for module_name in _EXCEPTION_SEARCH_MODULES:
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError:
                continue
            cls = getattr(module, name, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                return cls
        raise AssertionError(f"例外型 {name} がパッケージ内に見つかりません")

    return _exc


@pytest.fixture
def make_store(tmp_path):
    """SessionStore を作るファクトリを返す。

    引数なし: tmp_path/sessions.json を使う。
    ``name`` を渡すと同名ファイルを使う（永続化テストで再読み込みに使う）。
    """

    def _make(name: str = "sessions.json"):
        from interview_pipeline.store import SessionStore

        return SessionStore(tmp_path / name)

    return _make


@pytest.fixture
def drive_to(state_of, state_name):
    """セッションを正規の遷移経路で目標状態まで進める関数を返す。

    状態認識型: セッションの現在状態を読み、目標状態への経路
    （``_DRIVE_PATHS[target]``）上で現在状態より後ろのステップだけを
    実行する。これにより、同じセッションへ累積で複数回呼んでも
    通過済みステップへの後戻り遷移が発生しない。現在状態が経路に
    無い場合（``needs_reprocess`` 等）は従来どおり全ステップを実行する。
    """

    def _drive_to(store, session_id: str, target: str) -> None:
        path = _DRIVE_PATHS[target]
        try:
            current = state_name(store.get(session_id).state)
            # State（str Enum）の場合は素の状態名文字列へ寄せる。
            value = getattr(current, "value", None)
            if isinstance(value, str):
                current = value
        except Exception:
            current = None
        if current in path:
            steps = path[path.index(current) + 1 :]
        else:
            steps = path
        for step in steps:
            store.transition(session_id, state_of(step))

    return _drive_to


@pytest.fixture
def filekit():
    """ダミーファイル作成・マニフェスト組み立ての小物セットを返す。"""

    class _FileKit:
        @staticmethod
        def sha256_hex(data: bytes) -> str:
            return hashlib.sha256(data).hexdigest()

        @staticmethod
        def write(path: Path, data: bytes) -> Path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return path

        @staticmethod
        def manifest_dict(session_id: str, files: list[tuple[str, bytes]]) -> dict:
            """(ファイル名, 中身) のリストからマニフェスト dict を作る。"""
            return {
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

        @staticmethod
        def write_manifest(path: Path, manifest: dict) -> Path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return path

    return _FileKit()


class FakeTranscriber:
    """テスト側で定義するフェイクの文字起こしエンジン。

    ファイル名に ``system`` を含めば開始 0.0 のセグメント、``mic`` を含めば
    開始 0.5 のセグメントを返す（時系列マージの確認用）。
    ``fail_marker`` がパスに含まれる場合は RuntimeError を送出する。
    """

    def __init__(
        self,
        fail_marker: str | None = None,
        fail_message: str = "transcription failed (dummy)",
    ) -> None:
        self.fail_marker = fail_marker
        self.fail_message = fail_message
        self.calls: list[str] = []

    def transcribe(self, audio_path):
        from interview_pipeline.transcribe import Segment

        path_str = str(audio_path)
        self.calls.append(path_str)
        if self.fail_marker is not None and self.fail_marker in path_str:
            raise RuntimeError(self.fail_message)
        name = Path(path_str).name
        if "system" in name:
            return [Segment(start=0.0, end=1.0, text="SYSTEM-DUMMY-TEXT")]
        if "mic" in name:
            return [Segment(start=0.5, end=1.5, text="MIC-DUMMY-TEXT")]
        return [Segment(start=0.0, end=1.0, text="GENERIC-DUMMY-TEXT")]


@pytest.fixture
def make_transcriber():
    """FakeTranscriber を作るファクトリを返す。"""

    def _make(**kwargs) -> FakeTranscriber:
        return FakeTranscriber(**kwargs)

    return _make


@pytest.fixture
def make_event_log():
    """EventLog を作るファクトリを返す。"""

    def _make(path):
        from interview_pipeline.runner import EventLog

        return EventLog(path)

    return _make
