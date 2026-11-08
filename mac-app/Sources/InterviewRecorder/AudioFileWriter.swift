import AVFoundation

/// AAC（.m4a）書き出しの共通部品。書き出し形式は両系統とも
/// AAC・48kHz・モノラル・64kbps に統一する（docs/P3計画.md）。
///
/// 入力側のサンプルレートのままファイルを開いてはならない。
/// 実測（2026-11-08、MacBook Air のマイク入力で確定）:
/// 入力フォーマットは 16kHz であり、16kHz の AAC に対して
/// `AVEncoderBitRateKey` を指定すると `AVAudioFile` の生成が
/// `AudioConverterSetProperty(kAudioConverterEncodeBitRate)` の
/// 失敗（Code=560226676 '!dat'）で落ち、録音を開始できない。
/// 48kHz では同じ設定が通る。このため、ファイルは常に 48kHz で
/// 開き、入力バッファは `AudioFormatConverter` で変換してから書く
/// （要確認 #2「サンプル変換」・#3「マイク48kHz統一」の確定結果）。
enum AudioFileWriter {
    static let sampleRate: Double = 48_000
    static let channels: AVAudioChannelCount = 1
    static let bitRate = 64_000

    /// 書き出しファイルを開く。`processingFormat`（48kHz・モノラル・
    /// Float32）が、書き込みバッファの変換先になる。
    static func openFile(url: URL) throws -> AVAudioFile {
        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: Int(channels),
            AVEncoderBitRateKey: bitRate,
        ]
        return try AVAudioFile(forWriting: url, settings: settings)
    }
}

enum AudioWriteError: Error {
    /// 入力フォーマットから書き出し形式への変換器を作れなかった。
    case converterUnavailable
    /// バッファ変換に失敗した。
    case conversionFailed
}

/// 入力フォーマットから書き出しフォーマットへの変換器。
/// 変換状態（フィルタの履歴）を保持するため、録音ごとに
/// 1つ作って使い回すこと。
final class AudioFormatConverter {
    private let converter: AVAudioConverter
    let outputFormat: AVAudioFormat

    init?(from inputFormat: AVAudioFormat, to outputFormat: AVAudioFormat) {
        guard let converter = AVAudioConverter(from: inputFormat, to: outputFormat)
        else { return nil }
        self.converter = converter
        self.outputFormat = outputFormat
    }

    func convert(_ buffer: AVAudioPCMBuffer) throws -> AVAudioPCMBuffer {
        // 変換後フレーム数の上限を見積もる（サンプルレート比＋余裕）。
        let ratio = outputFormat.sampleRate / buffer.format.sampleRate
        let capacity =
            AVAudioFrameCount(ceil(Double(buffer.frameLength) * ratio)) + 1024
        guard
            capacity > 0,
            let out = AVAudioPCMBuffer(
                pcmFormat: outputFormat, frameCapacity: capacity)
        else {
            throw AudioWriteError.conversionFailed
        }
        var consumed = false
        var conversionError: NSError?
        let status = converter.convert(to: out, error: &conversionError) {
            _, inStatus in
            if consumed {
                inStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            inStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, conversionError == nil else {
            throw AudioWriteError.conversionFailed
        }
        return out
    }
}
