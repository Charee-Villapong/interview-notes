"""文字起こしインターフェースと話者合成。

話者ラベル: システム系統（相手）＝ ``SPEAKER_LABEL_SYSTEM``（``"B"``）、
マイク系統（自分）＝ ``SPEAKER_LABEL_MIC``（``"A"``）。
STT の実エンジン接続はモデル選定後のため、現状は
``UnavailableTranscriber``（常に未接続エラー）までを実装する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from interview_pipeline.errors import EngineNotConfiguredError
from interview_pipeline.states import ERROR_KIND_STT_NOT_CONFIGURED

__all__ = [
    "SPEAKER_LABEL_MIC",
    "SPEAKER_LABEL_SYSTEM",
    "EngineNotConfiguredError",
    "Segment",
    "Transcriber",
    "UnavailableTranscriber",
    "merge_tracks",
]

# 話者ラベル（正本 docs/状態管理.md「話者マッピング」）。
# システム系統（先方）＝ "B"、マイク系統（本人）＝ "A"。
SPEAKER_LABEL_SYSTEM = "B"
SPEAKER_LABEL_MIC = "A"


@dataclass
class Segment:
    start: float
    end: float
    text: str


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(self, audio_path: Path | str) -> list[Segment]: ...


class UnavailableTranscriber:
    """STT 未接続を表す実装。常に ``EngineNotConfiguredError`` を送出する。"""

    def transcribe(self, audio_path: Path | str) -> list[Segment]:
        raise EngineNotConfiguredError(ERROR_KIND_STT_NOT_CONFIGURED)


def merge_tracks(
    system_segments: list[Segment],
    mic_segments: list[Segment],
) -> list[tuple[str, Segment]]:
    """2系統のセグメントを時系列に合成し、話者ラベルを付ける。"""
    labelled: list[tuple[str, Segment]] = []
    labelled.extend((SPEAKER_LABEL_SYSTEM, segment) for segment in system_segments)
    labelled.extend((SPEAKER_LABEL_MIC, segment) for segment in mic_segments)
    labelled.sort(key=lambda item: item[1].start)
    return labelled
