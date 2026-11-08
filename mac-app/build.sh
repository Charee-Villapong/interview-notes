#!/bin/bash
# InterviewRecorder.app の組み立てと署名（Mac 上で実行する）。
# 手順の全体は mac-app/README.md を参照。
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="InterviewRecorder"
APP="${APP_NAME}.app"

swift build -c release

rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS"
cp ".build/release/${APP_NAME}" "${APP}/Contents/MacOS/${APP_NAME}"
cp "Resources/Info.plist" "${APP}/Contents/Info.plist"

# 署名は識別子ベースの指定要件（signing-requirement.txt）で行う。
# アドホック署名のまま要件なしで再署名すると、バイナリの同一性が
# 変わって TCC の許可が失われるため、この形を崩さないこと
# （docs/P3計画.md「必要権限とエンタイトルメント」参照）。
codesign --force --sign - --requirements signing-requirement.txt "${APP}"

echo "built: ${APP}"
codesign -d --entitlements - "${APP}" 2>/dev/null || true
