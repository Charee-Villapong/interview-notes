import AVFoundation

/// マイク（自分）のキャプチャ。AVAudioEngine の入力タップから
/// ファイルへ書き出す（docs/P3計画.md の F1 第一候補）。
/// 権限（TCC: Microphone）のリクエストは呼び出し側
/// （Permissions）が担う。
final class MicCapture {
    private let engine = AVAudioEngine()
    private var audioFile: AVAudioFile?
    private var converter: AudioFormatConverter?
    private(set) var isCapturing = false

    /// レベル（dBFS）の通知。無音検知に使う。
    var onLevel: ((Float) -> Void)?
    /// エラーの通知（失敗を黙って成功扱いにしない）。
    var onError: ((Error) -> Void)?

    func start(fileURL: URL) throws {
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        // 書き出しは AAC（.m4a）・48kHz・モノラルに統一する
        // （AudioFileWriter 参照。入力のサンプルレートで開くと
        // この Mac の 16kHz 入力ではビットレート設定が失敗する）。
        // 入力バッファは変換器で書き出し形式へ変換してから書く。
        let file = try AudioFileWriter.openFile(url: fileURL)
        guard
            let formatConverter = AudioFormatConverter(
                from: inputFormat, to: file.processingFormat)
        else {
            throw AudioWriteError.converterUnavailable
        }
        audioFile = file
        converter = formatConverter
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) {
            [weak self] buffer, _ in
            guard let self else { return }
            do {
                if let converted = try self.converter?.convert(buffer) {
                    try self.audioFile?.write(from: converted)
                }
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
        converter = nil
    }
}
