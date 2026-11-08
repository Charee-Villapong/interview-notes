import AppKit

/// メニューバー UI。録音開始／停止（手動停止が主たる終了経路）、
/// 状態表示、削除まわりの手動実行を担う。
final class StatusBarController: NSObject {
    private let statusItem: NSStatusItem
    private let controller: RecordingController
    private let deletionGuard: DeletionGuard
    private let statusMenuItem = NSMenuItem(title: "待機中", action: nil, keyEquivalent: "")
    private let startItem = NSMenuItem()
    private let stopItem = NSMenuItem()

    init(controller: RecordingController, deletionGuard: DeletionGuard) {
        self.controller = controller
        self.deletionGuard = deletionGuard
        self.statusItem = NSStatusBar.system.statusItem(
            withLength: NSStatusItem.variableLength
        )
        super.init()

        let menu = NSMenu()
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        menu.addItem(.separator())

        startItem.title = "録音開始"
        startItem.action = #selector(startRecording)
        startItem.target = self
        menu.addItem(startItem)

        stopItem.title = "停止"
        stopItem.action = #selector(stopRecording)
        stopItem.target = self
        menu.addItem(stopItem)

        menu.addItem(.separator())
        let retryItem = NSMenuItem(
            title: "未転送分を再送する", action: #selector(retryTransfers),
            keyEquivalent: ""
        )
        retryItem.target = self
        menu.addItem(retryItem)

        let deleteItem = NSMenuItem(
            title: "削除可のローカル音声を削除する",
            action: #selector(deleteEligible), keyEquivalent: ""
        )
        deleteItem.target = self
        menu.addItem(deleteItem)

        let overdueItem = NSMenuItem(
            title: "期限超過の未処理を確認する",
            action: #selector(showOverdue), keyEquivalent: ""
        )
        overdueItem.target = self
        menu.addItem(overdueItem)

        menu.addItem(.separator())
        let quitItem = NSMenuItem(
            title: "終了", action: #selector(quit), keyEquivalent: "q"
        )
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu

        controller.onPhaseChange = { [weak self] phase in
            DispatchQueue.main.async { self?.refresh(phase: phase) }
        }
        controller.onError = { [weak self] message in
            DispatchQueue.main.async { self?.showAlert(message) }
        }
        refresh(phase: controller.phase)
    }

    private func refresh(phase: RecordingController.Phase) {
        switch phase {
        case .idle:
            statusItem.button?.title = "面談レコーダー"
            statusMenuItem.title = "待機中"
            startItem.isEnabled = true
            stopItem.isEnabled = false
        case .recording:
            statusItem.button?.title = "● 録音中"
            statusMenuItem.title = "録音中"
            startItem.isEnabled = false
            stopItem.isEnabled = true
        case .grace:
            statusItem.button?.title = "● 録音中（無音・猶予中）"
            statusMenuItem.title = "無音が続いています（自動停止の猶予中）"
            startItem.isEnabled = false
            stopItem.isEnabled = true
        case .finalizing:
            statusItem.button?.title = "転送準備中"
            statusMenuItem.title = "録音を確定し、転送しています"
            startItem.isEnabled = false
            stopItem.isEnabled = false
        }
    }

    @objc private func startRecording() {
        Task { @MainActor in
            guard await Permissions.requestMicrophone() else {
                showAlert("マイクの許可がありません。システム設定で許可してください。")
                return
            }
            if !Permissions.screenCaptureGranted() {
                Permissions.requestScreenCapture()
                showAlert(
                    "画面収録の許可が必要です。システム設定 > "
                        + "プライバシーとセキュリティ > 画面収録 でこのアプリを許可し、"
                        + "もう一度「録音開始」を押してください。"
                )
                return
            }
            controller.start()
        }
    }

    @objc private func stopRecording() {
        controller.stopManually()
    }

    @objc private func retryTransfers() {
        controller.retryPendingTransfers()
    }

    @objc private func deleteEligible() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let deleted = self.deletionGuard.deleteEligibleCopies()
            DispatchQueue.main.async {
                self.showAlert(
                    deleted.isEmpty
                        ? "削除可のローカル音声はありませんでした。"
                        : "削除しました: \(deleted.joined(separator: ", "))"
                )
            }
        }
    }

    @objc private func showOverdue() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let overdue = self.deletionGuard.overdueUnprocessed()
            DispatchQueue.main.async {
                self.showAlert(
                    overdue.isEmpty
                        ? "期限超過の未処理はありません。"
                        : "期限超過の未処理（削除対象ではありません）: "
                            + overdue.map { $0.sessionId }.joined(separator: ", ")
                )
            }
        }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func showAlert(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "面談レコーダー"
        alert.informativeText = message
        alert.runModal()
    }
}
