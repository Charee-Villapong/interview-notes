"""文字起こしインターフェースと話者合成。

話者ラベル: システム系統（相手）＝ ``"B"``、マイク系統（自分）＝ ``"A"``。
STT の実エンジン接続はモデル選定後のため、現状は
``UnavailableTranscriber``（常に未接続エラー）までを実装する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from interview_pipeline.errors import EngineNotConfiguredError

__all__ = [
    "EngineNotConfiguredError",
    "Segment",
    "Transcriber",
    "UnavailableTranscriber",
    "merge_tracks",
]


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
        raise EngineNotConfiguredError("stt_not_configured")


def merge_tracks(
    system_segments: list[Segment],
    mic_segments: list[Segment],
) -> list[tuple[str, Segment]]:
    """2系統のセグメントを時系列に合成し、話者ラベルを付ける。"""
    labelled: list[tuple[str, Segment]] = []
    labelled.extend(("B", segment) for segment in system_segments)
    labelled.extend(("A", segment) for segment in mic_segments)
    labelled.sort(key=lambda item: item[1].start)
    return labelled
