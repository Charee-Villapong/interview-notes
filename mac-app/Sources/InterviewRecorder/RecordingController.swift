import Foundation

/// 録音の状態機械。終了検知は「手動停止が主、5分間無音で自動停止、
/// 自動停止後の再開猶予5分、安全弁3時間」（確定仕様 J-8。
/// 対応表は docs/P3計画.md）。
///
/// 猶予中の扱い: ファイルは開いたままにし、再開（レベル回復）が
/// あればそのまま書き足す。猶予が過ぎれば停止として確定する。
final class RecordingController {
    enum Phase {
        case idle
        case recording
        /// 自動停止の猶予中（同一セッション扱い）。
        case grace
        case finalizing
    }

    private let config: AppConfig
    private let store: SessionStore
    private let transfer: TransferClient
    private let systemCapture = SystemAudioCapture()
    private let micCapture = MicCapture()
    private let silenceMonitor: SilenceMonitor

    private(set) var phase: Phase = .idle
    private var currentSessionId: String?
    private var recordingStartedAt: Date?
    private var graceDeadline: Date?
    private var tickTimer: Timer?

    /// 状態変化の通知（メニュー表示の更新用）。
    var onPhaseChange: ((Phase) -> Void)?
    /// エラーの通知（失敗を黙って成功扱いにしない）。
    var onError: ((String) -> Void)?

    init(config: AppConfig, store: SessionStore) {
        self.config = config
        self.store = store
        self.transfer = TransferClient(config: config)
        self.silenceMonitor = SilenceMonitor(
            thresholdDb: Float(config.silenceThresholdDb)
        )
    }

    var isRecording: Bool { phase == .recording || phase == .grace }

    // MARK: - 開始と停止

    func start() {
        guard phase == .idle else { return }
        let sessionId = store.nextSessionId()
        let sessionDir = AppConfig.recordingsDir()
            .appendingPathComponent(sessionId, isDirectory: true)
        do {
            try FileManager.default.createDirectory(
                at: sessionDir, withIntermediateDirectories: true
            )
            let systemURL = sessionDir
                .appendingPathComponent("system_\(sessionId).m4a")
            let micURL = sessionDir.appendingPathComponent("mic_\(sessionId).m4a")

            systemCapture.onLevel = { [weak self] db in
                self?.silenceMonitor.noteLevel(db, track: .system)
            }
            systemCapture.onError = { [weak self] error in
                self?.onError?("system capture: \(error)")
            }
            micCapture.onLevel = { [weak self] db in
                self?.silenceMonitor.noteLevel(db, track: .mic)
            }
            micCapture.onError = { [weak self] error in
                self?.onError?("mic capture: \(error)")
            }

            try micCapture.start(fileURL: micURL)
            Task { [weak self] in
                guard let self else { return }
                do {
                    try await self.systemCapture.start(fileURL: systemURL)
                } catch {
                    self.rollbackFailedStart(sessionId: sessionId)
                    self.onError?("system capture start: \(error)")
                }
            }

            currentSessionId = sessionId
            recordingStartedAt = Date()
            silenceMonitor.reset()
            setPhase(.recording)
            tickTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) {
                [weak self] _ in
                self?.tick()
            }
        } catch {
            onError?("recording start: \(error)")
        }
    }

    /// システム音声の開始が非同期に失敗したときの巻き戻し。
    /// 録音中表示・タイマ・セッション情報を開始前の状態へ戻す。
    /// この時点のセッションはストア未登録のため、作りかけの
    /// セッションフォルダも残さない。
    private func rollbackFailedStart(sessionId: String) {
        guard phase == .recording, currentSessionId == sessionId else { return }
        tickTimer?.invalidate()
        tickTimer = nil
        micCapture.stop()
        currentSessionId = nil
        recordingStartedAt = nil
        graceDeadline = nil
        silenceMonitor.reset()
        let sessionDir = AppConfig.recordingsDir()
            .appendingPathComponent(sessionId, isDirectory: true)
        try? FileManager.default.removeItem(at: sessionDir)
        setPhase(.idle)
    }

    /// 手動停止（主たる終了経路）。即時に転送対象にする
    /// （正本 docs/状態管理.md の遷移条件）。
    func stopManually() {
        guard isRecording else { return }
        finalize()
    }

    // MARK: - 監視（1秒間隔）

    private func tick() {
        guard let startedAt = recordingStartedAt else { return }
        let now = Date()
        // 安全弁: 3時間で強制停止する。
        if now.timeIntervalSince(startedAt) >= Double(config.maxDurationSeconds) {
            finalize()
            return
        }
        switch phase {
        case .recording:
            if silenceMonitor.silentDuration(now: now)
                >= Double(config.silenceStopSeconds) {
                graceDeadline = now.addingTimeInterval(
                    Double(config.resumeGraceSeconds)
                )
                setPhase(.grace)
            }
        case .grace:
            // 猶予内に音が戻れば同一セッションのまま録音を続ける。
            if silenceMonitor.silentDuration(now: now)
                < Double(config.silenceStopSeconds) {
                graceDeadline = nil
                setPhase(.recording)
            } else if let deadline = graceDeadline, now >= deadline {
                finalize()
            }
        default:
            break
        }
    }

    // MARK: - 確定と転送

    private func finalize() {
        guard let sessionId = currentSessionId,
              let startedAt = recordingStartedAt else { return }
        setPhase(.finalizing)
        tickTimer?.invalidate()
        tickTimer = nil
        graceDeadline = nil

        let sessionDir = AppConfig.recordingsDir()
            .appendingPathComponent(sessionId, isDirectory: true)
        let systemName = "system_\(sessionId).m4a"
        let micName = "mic_\(sessionId).m4a"

        Task { [weak self] in
            guard let self else { return }
            await self.systemCapture.stop()
            self.micCapture.stop()

            var session = MacSession(
                sessionId: sessionId,
                state: "recorded",
                localPhase: "finalizing",
                createdAt: SessionStore.isoString(from: startedAt),
                resumeState: nil,
                errorKind: nil,
                finalSavedAt: nil,
                macCopyDeletedAt: nil,
                systemFileName: systemName,
                micFileName: micName
            )
            do {
                let manifest = try ManifestBuilder.build(
                    sessionId: sessionId,
                    fileURLs: [
                        sessionDir.appendingPathComponent(systemName),
                        sessionDir.appendingPathComponent(micName),
                    ]
                )
                try ManifestBuilder.write(
                    manifest,
                    to: sessionDir.appendingPathComponent("manifest.json")
                )
            } catch {
                session.state = "needs_reprocess"
                session.resumeState = "transfer_pending"
                // Mac ローカル記録専用の種別（VM 側の語彙には送らない）。
                session.errorKind = "manifest_failed"
                session.localPhase = "upload_failed"
                self.store.upsert(session)
                self.onError?("manifest: \(error)")
                self.setPhase(.idle)
                return
            }

            session.state = "transfer_pending"
            session.localPhase = "ready_to_transfer"
            self.store.upsert(session)
            self.upload(session: session, sessionDir: sessionDir)
            self.setPhase(.idle)
        }
    }

    /// 転送（登録→配置→受信検証）。失敗したセッションは未処理として
    /// 残し、後から `retryPendingTransfers()` で再開できる。
    private func upload(session: MacSession, sessionDir: URL) {
        var working = session
        working.state = "transferring"
        working.localPhase = "uploading"
        store.upsert(working)
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            do {
                try self.transfer.transfer(session: working, sessionDir: sessionDir)
                working.state = "received"
                working.localPhase = "uploaded"
                working.errorKind = nil
                working.resumeState = nil
            } catch {
                working.state = "needs_reprocess"
                working.resumeState = "transfer_pending"
                working.errorKind = "transfer_failed"
                working.localPhase = "upload_failed"
                self.onError?("transfer: \(error)")
            }
            self.store.upsert(working)
        }
    }

    /// 転送に失敗したまま残っているセッションを再転送する。
    func retryPendingTransfers() {
        for session in store.sessions
        where session.state == "needs_reprocess"
            && session.resumeState == "transfer_pending" {
            let sessionDir = AppConfig.recordingsDir()
                .appendingPathComponent(session.sessionId, isDirectory: true)
            upload(session: session, sessionDir: sessionDir)
        }
    }

    private func setPhase(_ phase: Phase) {
        self.phase = phase
        onPhaseChange?(phase)
    }
}
