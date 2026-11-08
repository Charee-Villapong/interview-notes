import Foundation

/// Mac 側コピーの削除ガード連携。削除は VM 側の判定
/// （`mac-eligible`）が可を返したセッションに限る。
/// 未処理は日数に関わらず削除しない（正本 docs/状態管理.md
/// 「削除の条件」。期限（最長7日）は許可条件ではなく上限）。
struct DeletionGuard {
    let transfer: TransferClient
    let store: SessionStore

    /// 削除可と確認できたセッションのローカル音声を削除する。
    /// 戻り値は削除したセッション ID。
    /// 注意: VM 側へ削除完了を申告し `closed` へ進める手段は現行 CLI
    /// に無く、連携事項として docs/P3計画.md に残してある。
    /// ここでは Mac 側メタデータへの記録に留める。
    func deleteEligibleCopies() -> [String] {
        var deleted: [String] = []
        for session in store.sessions where session.macCopyDeletedAt == nil {
            let sessionDir = AppConfig.recordingsDir()
                .appendingPathComponent(session.sessionId, isDirectory: true)
            guard FileManager.default.fileExists(atPath: sessionDir.path) else {
                continue
            }
            guard transfer.macEligible(sessionId: session.sessionId) else {
                continue
            }
            do {
                try FileManager.default.removeItem(at: sessionDir)
            } catch {
                continue
            }
            var updated = session
            updated.macCopyDeletedAt = SessionStore.isoString(from: Date())
            updated.localPhase = "deleted"
            store.upsert(updated)
            deleted.append(session.sessionId)
        }
        return deleted
    }

    /// 期限超過の未処理（録音から7日超・`final_saved` 未到達）を
    /// 可視化のために返す。削除のためには使わない。
    func overdueUnprocessed(now: Date = Date()) -> [MacSession] {
        let remote = transfer.remoteStates()
        return store.sessions.filter { session in
            guard session.finalSavedAt == nil,
                  remote[session.sessionId] != "final_saved",
                  remote[session.sessionId] != "deletable",
                  remote[session.sessionId] != "closed",
                  let created = SessionStore.date(fromIso: session.createdAt)
            else {
                return false
            }
            return now.timeIntervalSince(created) > 7 * 24 * 60 * 60
        }
    }
}
