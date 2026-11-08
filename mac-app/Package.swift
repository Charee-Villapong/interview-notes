// swift-tools-version: 5.9
import PackageDescription

// Mac側ツール（完成形G）の雛形。計画は docs/P3計画.md、ビルドと
// 試行の手順は mac-app/README.md を参照。
// .app バンドルへの組み立てと署名は build.sh が担う。
let package = Package(
    name: "InterviewRecorder",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "InterviewRecorder",
            path: "Sources/InterviewRecorder"
        )
    ]
)
