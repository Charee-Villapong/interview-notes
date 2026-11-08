import Foundation

/// 無音検知。両系統（system / mic）のレベルを監視し、両方とも
/// 閾値以下が続いた時間を数える。自動停止の判定は
/// RecordingController がこの値をポーリングして行う。
final class SilenceMonitor {
    enum Track {
        case system
        case mic
    }

    let thresholdDb: Float
    private var lastAudibleAt: [Track: Date] = [:]

    init(thresholdDb: Float) {
        self.thresholdDb = thresholdDb
    }

    func reset(at date: Date = Date()) {
        lastAudibleAt = [.system: date, .mic: date]
    }

    /// レベル（dBFS）を記録する。閾値を超えたら「音があった」時刻を更新する。
    func noteLevel(_ db: Float, track: Track, at date: Date = Date()) {
        if db > thresholdDb {
            lastAudibleAt[track] = date
        }
    }

    /// 両系統とも無音が続いている時間（秒）。
    /// 両方とも静かな期間は「最後にどちらかで音があった時刻」から数える。
    func silentDuration(now: Date = Date()) -> TimeInterval {
        guard let system = lastAudibleAt[.system], let mic = lastAudibleAt[.mic]
        else {
            return 0
        }
        let lastAudible = max(system, mic)
        return now.timeIntervalSince(lastAudible)
    }
}
