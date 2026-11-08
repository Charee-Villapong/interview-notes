import AVFoundation

/// マイク（自分）のキャプチャ。AVAudioEngine の入力タップから
/// ファイルへ書き出す（docs/P3計画.md の F1 第一候補）。
/// 権限（TCC: Microphone）のリクエストは呼び出し側
/// （Permissions）が担う。
final class MicCapture {
    private let engine = AVAudioEngine()
    private var audioFile: AVAudioFile?
    private(set) var isCapturing = false

    /// レベル（dBFS）の通知。無音検知に使う。
    var onLevel: ((Float) -> Void)?
    /// エラーの通知（失敗を黙って成功扱いにしない）。
    var onError: ((Error) -> Void)?

    func start(fileURL: URL) throws {
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        // 書き出しは AAC（.m4a）。サンプルレートは入力に合わせ、
        // 48kHz への統一（コンバータ要否）は F1 の実測で確定する。
        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: inputFormat.sampleRate,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 64_000,
        ]
        audioFile = try AVAudioFile(
            forWriting: fileURL, settings: settings,
            commonFormat: inputFormat.commonFormat,
            interleaved: inputFormat.isInterleaved
        )
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) {
            [weak self] buffer, _ in
            guard let self else { return }
            do {
                try self.audioFile?.write(from: buffer)
            } catch {
                self.onError?(error)
            }
            self.onLevel?(AudioLevel.rmsDb(of: buffer))
        }
        engine.prepare()
        try engine.start()
        isCapturing = true
    }

    func stop() {
        if isCapturing {
            engine.stop()
            engine.inputNode.removeTap(onBus: 0)
        }
        isCapturing = false
        audioFile = nil
    }
}
