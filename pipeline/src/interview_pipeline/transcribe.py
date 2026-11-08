"""文字起こしインターフェースと話者合成。

話者ラベル: システム系統（相手）＝ ``SPEAKER_LABEL_SYSTEM``（``"B"``）、
マイク系統（自分）＝ ``SPEAKER_LABEL_MIC``（``"A"``）。
実エンジンは ``FasterWhisperTranscriber``（faster-whisper。V1 接続。
契約は pipeline/SPEC.md「V1 追加契約」）。``UnavailableTranscriber`` は
エンジン未設定時の実装として残す。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from interview_pipeline.errors import EngineNotConfiguredError
from interview_pipeline.states import ERROR_KIND_STT_NOT_CONFIGURED

__all__ = [
    "DEFAULT_STT_MODEL",
    "SPEAKER_LABEL_MIC",
    "SPEAKER_LABEL_SYSTEM",
    "EngineNotConfiguredError",
    "FasterWhisperTranscriber",
    "Segment",
    "Transcriber",
    "UnavailableTranscriber",
    "merge_tracks",
]

# 話者ラベル（正本 docs/状態管理.md「話者マッピング」）。
# システム系統（先方）＝ "B"、マイク系統（本人）＝ "A"。
SPEAKER_LABEL_SYSTEM = "B"
SPEAKER_LABEL_MIC = "A"

# V1 の選定案モデル（最終確定は J-9。本人の判断）。
DEFAULT_STT_MODEL = "large-v3-turbo"

# モデル生成関数の型（テストでの差し替え点）。
ModelFactory = Callable[..., Any]


def _default_model_factory(
    model_name: str, *, compute_type: str, cpu_threads: int
) -> Any:
    """faster-whisper のモデルを生成する（import は遅延させる）。"""
    from faster_whisper import WhisperModel

    return WhisperModel(
        model_name,
        device="cpu",
        compute_type=compute_type,
        cpu_threads=cpu_threads,
    )


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


class FasterWhisperTranscriber:
    """faster-whisper を実エンジンとする文字起こし実装。

    モデルの読み込みは初回 ``transcribe`` 時まで遅延し、読み込んだ
    モデルは再利用する。``faster_whisper`` が import できない環境では
    「未設定」として ``EngineNotConfiguredError`` を送出する。
    推論条件の既定値は V1 実測と同一（int8／CPU 4スレッド／日本語）。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_STT_MODEL,
        *,
        compute_type: str = "int8",
        cpu_threads: int = 4,
        language: str = "ja",
        beam_size: int = 5,
        vad_filter: bool = False,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.model_name = model_name
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self._model_factory = model_factory or _default_model_factory
        self._model: Any = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            try:
                self._model = self._model_factory(
                    self.model_name,
                    compute_type=self.compute_type,
                    cpu_threads=self.cpu_threads,
                )
            except ImportError as exc:
                raise EngineNotConfiguredError(ERROR_KIND_STT_NOT_CONFIGURED) from exc
        return self._model

    def transcribe(self, audio_path: Path | str) -> list[Segment]:
        model = self._ensure_model()
        segments, _info = model.transcribe(
            str(audio_path),
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
        )
        return [
            Segment(start=float(seg.start), end=float(seg.end), text=seg.text)
            for seg in segments
        ]


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
