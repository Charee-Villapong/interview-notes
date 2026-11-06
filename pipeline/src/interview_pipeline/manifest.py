"""転送マニフェスト。

マニフェストは ``session_id`` と、ファイルごとの ``name`` / ``size`` /
``sha256`` を持つ。系統はファイル名の接頭辞で区別する
（``system`` ＝相手、``mic`` ＝自分）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileEntry:
    name: str
    size: int
    sha256: str


@dataclass
class Manifest:
    session_id: str
    files: list[FileEntry] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> Manifest:
        if not isinstance(data, dict):
            raise ValueError("manifest must be a dict")
        if "session_id" not in data:
            raise ValueError("manifest is missing 'session_id'")
        if "files" not in data:
            raise ValueError("manifest is missing 'files'")
        files_data = data["files"]
        if not isinstance(files_data, list):
            raise ValueError("manifest 'files' must be a list")
        files: list[FileEntry] = []
        for item in files_data:
            if not isinstance(item, dict):
                raise ValueError("manifest file entry must be a dict")
            for key in ("name", "size", "sha256"):
                if key not in item:
                    raise ValueError(f"manifest file entry is missing {key!r}")
            files.append(
                FileEntry(
                    name=str(item["name"]),
                    size=int(item["size"]),
                    sha256=str(item["sha256"]),
                )
            )
        return cls(session_id=str(data["session_id"]), files=files)

    @classmethod
    def load(cls, path: Path | str) -> Manifest:
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(json.loads(text))

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "files": [
                {"name": f.name, "size": f.size, "sha256": f.sha256} for f in self.files
            ],
        }
