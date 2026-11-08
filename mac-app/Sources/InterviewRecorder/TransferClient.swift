import Foundation

/// VM 側パイプラインとの受け渡し。既存の SSH 経路で VM の CLI
/// （`python -m interview_pipeline ...`）とステージング配置を行う。
/// 接続先・配置先はすべて AppConfig から読む（実値を書かない）。
/// フローの正本は docs/P3計画.md「転送フロー」。
struct TransferClient {
    let config: AppConfig

    struct TransferError: Error {
        var message: String
    }

    // MARK: - VM 側 CLI の実行

    /// パイプライン CLI のリモートコマンド文字列を組み立てる。
    private func pipelineCommand(_ arguments: String) -> String {
        var env = "INTERVIEW_DATA_DIR=\(shellQuote(config.remoteDataDir))"
        if let pythonPath = config.remotePythonPath, !pythonPath.isEmpty {
            env += " PYTHONPATH=\(shellQuote(pythonPath))"
        }
        return "cd \(shellQuote(config.remotePipelineDir)) && \(env) "
            + "\(config.remotePipelinePython) -m interview_pipeline \(arguments)"
    }

    private func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    @discardableResult
    private func runProcess(_ launchPath: String, _ arguments: [String]) -> (Int32, String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: launchPath)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
        } catch {
            return (-1, "")
        }
        process.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }

    private func runSSH(_ remoteCommand: String) -> (Int32, String) {
        runProcess("/usr/bin/ssh", [config.sshHostAlias, remoteCommand])
    }

    // MARK: - 転送フロー

    /// VM 側へセッションを登録する（冪等。初期状態は `recorded`）。
    func register(session: MacSession) throws {
        let command = pipelineCommand(
            "register \(session.sessionId) --created-at \(shellQuote(session.createdAt))"
        )
        let (status, output) = runSSH(command)
        guard status == 0 else {
            throw TransferError(message: "register failed: \(output)")
        }
    }

    /// 音声2ファイルとマニフェストをステージングへ配置する。
    /// 再転送時は同じ場所へ置き直し、VM 側で置き換えられる。
    func upload(sessionDir: URL, sessionId: String) throws {
        let remoteStaging = "\(config.remoteDataDir)/staging/\(sessionId)"
        let (mkdirStatus, mkdirOutput) = runSSH("mkdir -p \(shellQuote(remoteStaging))")
        guard mkdirStatus == 0 else {
            throw TransferError(message: "staging mkdir failed: \(mkdirOutput)")
        }
        let files = [
            sessionDir.appendingPathComponent("manifest.json").path,
        ] + audioFiles(in: sessionDir).map { $0.path }
        let target = "\(config.sshHostAlias):\(shellQuote(remoteStaging + "/"))"
        let (status, output) = runProcess("/usr/bin/scp", files + [target])
        guard status == 0 else {
            throw TransferError(message: "scp failed: \(output)")
        }
    }

    /// VM 側の受信検証を要求する。転送完了は VM 側でサイズ＋
    /// チェックサムが一致した時のみ（終了コード 0）。
    func receive(sessionId: String) -> Bool {
        let (status, _) = runSSH(pipelineCommand("receive \(sessionId)"))
        return status == 0
    }

    /// register → upload → receive を一括で行う。
    /// 失敗時は例外（呼び出し側で未処理として記録する）。
    func transfer(session: MacSession, sessionDir: URL) throws {
        try register(session: session)
        try upload(sessionDir: sessionDir, sessionId: session.sessionId)
        guard receive(sessionId: session.sessionId) else {
            throw TransferError(message: "receive verification failed")
        }
    }

    // MARK: - 状態照会と削除可否

    /// VM 側の全セッション状態を照会する（`session_id -> state`）。
    func remoteStates() -> [String: String] {
        let (status, output) = runSSH(pipelineCommand("status"))
        guard status == 0 else { return [:] }
        var result: [String: String] = [:]
        for line in output.split(separator: "\n") {
            let parts = line.split(separator: " ", maxSplits: 1)
            if parts.count == 2 {
                result[String(parts[0])] = String(parts[1])
            }
        }
        return result
    }

    /// Mac 側コピーの削除可否を VM 側に照会する。
    /// 判定条件の正本は docs/状態管理.md「削除の条件」で、
    /// Mac 側では再解釈しない。
    func macEligible(sessionId: String) -> Bool {
        let (status, _) = runSSH(pipelineCommand("mac-eligible \(sessionId)"))
        return status == 0
    }

    private func audioFiles(in sessionDir: URL) -> [URL] {
        let contents = (try? FileManager.default
            .contentsOfDirectory(at: sessionDir, includingPropertiesForKeys: nil)) ?? []
        return contents
            .filter { $0.lastPathComponent != "manifest.json" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }
}
