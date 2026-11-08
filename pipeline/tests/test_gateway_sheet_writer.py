"""面談一覧の行追加が sheet-writer tool 経由であることのテスト。

根拠: シート書き込みの見切れ防止を構造で担保する方針（2026-11-08 本人決定）
により、``CliGoogleGateway.append_sheet_row`` は Sheets API の直接 append
ではなく sheet-writer tool の CLI を subprocess で呼ぶ。

- 成功時: 戻り値は sheet-writer が報告した行番号。渡した行がそのまま
  --row の JSON として tool に渡り、一時設定には対象 ID・タブ・列ポリシー
  （A〜C は fit、D〜F は wrap）が入る。
- 失敗時（tool が非0終了・src が無い）: GoogleGatewayError。
- 実接続はしない（フェイクの sheet_writer パッケージを tmp に置く）。
"""

from __future__ import annotations

import json

import pytest

from interview_pipeline.errors import GoogleGatewayError
from interview_pipeline.gateway import CliGoogleGateway
from interview_pipeline.google_config import load_google_config

FAKE_CLI_OK = """
import json
import os
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    record = os.environ.get("FAKE_SHEET_WRITER_RECORD")
    if record:
        config_path = argv[argv.index("--config") + 1]
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
        with open(record, "w", encoding="utf-8") as fh:
            json.dump({"argv": argv, "config": config}, fh, ensure_ascii=False)
    print("追加しました: rows=7 widths={} heights={}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

FAKE_CLI_FAIL = """
import sys


def main(argv=None):
    print("boom", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
"""

VALUES = ["2026-11-10", "ダミー社", "オンライン", "", "", "https://example.com/dummy"]


def _make_fake_src(tmp_path, cli_source: str):
    package = tmp_path / "fake_src" / "sheet_writer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(cli_source, encoding="utf-8")
    return tmp_path / "fake_src"


def _gateway(tmp_path, sheet_writer_src: str) -> CliGoogleGateway:
    data = {
        "cli_path": "/dummy/cli",
        "sheets_spreadsheet_id": "dummy-spreadsheet-id",
        "sheet_writer_src": sheet_writer_src,
    }
    path = tmp_path / "google_config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return CliGoogleGateway(load_google_config(path))


def test_append_sheet_row_goes_through_sheet_writer(tmp_path, monkeypatch):
    src = _make_fake_src(tmp_path, FAKE_CLI_OK)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_SHEET_WRITER_RECORD", str(record))
    gateway = _gateway(tmp_path, str(src))

    row_number = gateway.append_sheet_row("dummy-spreadsheet-id", "面談一覧", VALUES)

    assert row_number == 7
    recorded = json.loads(record.read_text(encoding="utf-8"))
    argv = recorded["argv"]
    assert argv[0] == "append"
    passed_row = json.loads(argv[argv.index("--row") + 1])
    assert passed_row == VALUES
    targets = recorded["config"]["targets"]
    (target,) = targets.values()
    assert target["spreadsheet_id"] == "dummy-spreadsheet-id"
    assert target["tab"] == "面談一覧"
    assert target["columns"]["A"] == {"mode": "fit"}
    assert target["columns"]["D"] == {"mode": "wrap", "width_px": 300}
    assert target["columns"]["F"] == {"mode": "wrap", "width_px": 300}


def test_append_sheet_row_failure_raises_gateway_error(tmp_path):
    src = _make_fake_src(tmp_path, FAKE_CLI_FAIL)
    gateway = _gateway(tmp_path, str(src))
    with pytest.raises(GoogleGatewayError):
        gateway.append_sheet_row("dummy-spreadsheet-id", "面談一覧", VALUES)


def test_append_sheet_row_missing_sheet_writer_raises_gateway_error(tmp_path):
    gateway = _gateway(tmp_path, str(tmp_path / "no-such-src"))
    with pytest.raises(GoogleGatewayError):
        gateway.append_sheet_row("dummy-spreadsheet-id", "面談一覧", VALUES)
