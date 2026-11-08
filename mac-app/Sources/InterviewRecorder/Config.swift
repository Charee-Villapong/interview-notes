import Foundation

/// ローカル設定。接続先・配置先などの環境依存値はすべてここから読む。
/// コードとリポジトリに実値を書かない（実値は Mac 上のローカル設定
/// ファイルにだけ置く。雛形は mac-app/config.example.json）。
///
/// 読み込み先（優先順）:
/// 1. 環境変数 `INTERVIEW_RECORDER_CONFIG` が指すファイル
/// 2. `~/Library/Application Support/InterviewRecorder/config.json`
struct AppConfig: Codable {
    /// SSH の接続先（~/.ssh/config の Host エイリアス名）。
    var sshHostAlias: String
    /// VM 側のデータ領域（VM 側の `INTERVIEW_DATA_DIR` と同じ場所）。
    var remoteDataDir: String
    /// VM 側リポジトリの `pipeline/` ディレクトリのパス。
    var remotePipelineDir: String
    /// VM 側でパイプラインを実行する Python のコマンド名。
    var remotePipelinePython: String = "python3"
    /// VM 側パイプラインのモジュール検索パス（`PYTHONPATH`）。
    /// VM 側はパッケージをインストールせず `src` レイアウトのまま
    /// 実行するのが正本の運用（cron の実行形式と同一）ため、未指定
    /// （nil）でなく値を入れること。相対指定は `remotePipelineDir`
    /// へ移動した後の相対として解釈される。
    var remotePythonPath: String? = nil
    /// 無音とみなすレベル（dBFS）。校正は F1 の実測で行う。
    var silenceThresholdDb: Double = -50.0
    /// 両系統とも無音がこの秒数続いたら自動停止の対象にする（確定値 300 = 5分）。
    var silenceStopSeconds: Int = 300
    /// 自動停止後の再開猶予（秒。確定値 300 = 5分）。
    var resumeGraceSeconds: Int = 300
    /// 安全弁の上限（秒。確定値 10800 = 3時間）。
    var maxDurationSeconds: Int = 10800

    static func load() throws -> AppConfig {
        let url = configFileURL()
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(AppConfig.self, from: data)
    }

    static func configFileURL() -> URL {
        if let path = ProcessInfo.processInfo.environment["INTERVIEW_RECORDER_CONFIG"],
           !path.isEmpty {
            return URL(fileURLWithPath: path)
        }
        return appSupportDir().appendingPathComponent("config.json")
    }

    /// アプリのデータ領域（設定・セッション記録・録音の置き場）。
    static func appSupportDir() -> URL {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        ).first!
        let dir = base.appendingPathComponent("InterviewRecorder", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true
        )
        return dir
    }

    /// 録音ファイルの置き場（データ領域配下。リポジトリには置かない）。
    static func recordingsDir() -> URL {
        let dir = appSupportDir().appendingPathComponent("recordings", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true
        )
        return dir
    }
}
