import AVFoundation

/// PCM バッファから RMS レベル（dBFS）を計算する共通処理。
enum AudioLevel {
    static func rmsDb(of buffer: AVAudioPCMBuffer) -> Float {
        guard buffer.frameLength > 0 else { return -160.0 }
        if let channels = buffer.floatChannelData {
            let count = Int(buffer.frameLength)
            var sum: Float = 0
            let samples = channels[0]
            for i in 0..<count {
                let value = samples[i]
                sum += value * value
            }
            let rms = sqrt(sum / Float(count))
            return rms > 0 ? 20 * log10(rms) : -160.0
        }
        // 非 float 形式は F1 の実測で扱いを確定する（当面は無音扱い）。
        return -160.0
    }
}
