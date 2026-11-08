import Foundation

/// Mac 側のセッション記録。`state` には VM 側12状態の語彙
/// （docs/状態管理.md）だけを入れ、録音中などの Mac 内側の細かい
/// 状態は `localPhase` に分離する（対応表は docs/P3計画.md）。
/// フィールド名は VM 側 `Session`（pipeline の store.py）に揃える。
struct MacSession: Codable {
    var sessionId: String
    /// VM 側12状態の語彙。録音中（VM 未登録）は "unregistered" とする。
    var state: String
    /// Mac 内側の補助状態: recording / grace / finalizing /
    /// ready_to_transfer / uploading / uploaded / upload_failed /
    /// mirrored / deletion_ready / deleted
    var localPhase: String
    /// 録音開始日時（ISO8601）。VM 側 `created_at` に対応する。
    var createdAt: String
    var resumeState: String?
    var errorKind: String?
    var finalSavedAt: String?
    var macCopyDeletedAt: String?
    var systemFileName: String
    var micFileName: String
}

/// セッション記録の永続化（JSON ファイル。再起動を挟んでも再開できる）。
final class SessionStore {
    private(set) var sessions: [MacSession] = []
    private let fileURL: URL

    init(fileURL: URL = AppConfig.appSupportDir()
        .appendingPathComponent("sessions.json")) {
        self.fileURL = fileURL
        load()
    }

    func load() {
        guard let data = try? Data(contentsOf: fileURL),
              let decoded = try? JSONDecoder().decode([MacSession].self, from: data)
        else {
            sessions = []
            return
        }
        sessions = decoded
    }

    func save() {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(sessions) else { return }
        try? data.write(to: fileURL, options: [.atomic])
    }

    func upsert(_ session: MacSession) {
        if let index = sessions.firstIndex(where: { $0.sessionId == session.sessionId }) {
            sessions[index] = session
        } else {
            sessions.append(session)
        }
        save()
    }

    func session(_ sessionId: String) -> MacSession? {
        sessions.first(where: { $0.sessionId == sessionId })
    }

    /// 面談日付ベースのセッション ID を採番する（`YYYY-MM-DD_NN`。
    /// 正本 docs/状態管理.md「セッション識別子」）。
    func nextSessionId(for date: Date = Date()) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        let day = formatter.string(from: date)
        let count = sessions.filter { $0.sessionId.hasPrefix(day + "_") }.count
        return String(format: "%@_%02d", day, count + 1)
    }

    static func isoString(from date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    static func date(fromIso string: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: string)
    }
}
