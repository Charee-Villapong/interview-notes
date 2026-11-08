import AppKit
import AVFoundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusBarController: StatusBarController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let config = try AppConfig.load()
            let store = SessionStore()
            let controller = RecordingController(config: config, store: store)
            let deletionGuard = DeletionGuard(
                transfer: TransferClient(config: config), store: store
            )
            statusBarController = StatusBarController(
                controller: controller, deletionGuard: deletionGuard
            )
            // 起動時に、転送に失敗したまま残っている分を再開する。
            controller.retryPendingTransfers()
        } catch {
            // 設定が読めない場合は理由を表示して終了する
            // （黙って起動したことにしない）。
            let alert = NSAlert()
            alert.messageText = "設定ファイルを読み込めませんでした"
            alert.informativeText = "\(error)\n\n"
                + "mac-app/README.md の手順で config.json を配置してください。"
            alert.runModal()
            NSApp.terminate(nil)
        }
    }
}

/// TCC 権限の確認とリクエスト。付与そのものは本人の手動操作であり、
/// ここではダイアログの表示と、未付与時の案内までを行う。
enum Permissions {
    static func requestMicrophone() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return true
        case .notDetermined:
            return await AVCaptureDevice.requestAccess(for: .audio)
        default:
            return false
        }
    }

    static func screenCaptureGranted() -> Bool {
        CGPreflightScreenCaptureAccess()
    }

    /// 画面収録の許可ダイアログを出す（未決定の場合のみ表示される。
    /// 付与はシステム設定での本人の操作が必要）。
    static func requestScreenCapture() {
        if !CGPreflightScreenCaptureAccess() {
            CGRequestScreenCaptureAccess()
        }
    }
}
