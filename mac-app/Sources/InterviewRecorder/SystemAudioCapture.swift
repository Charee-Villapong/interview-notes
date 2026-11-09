import AVFoundation
import CoreMedia
import ScreenCaptureKit

/// システム音声（相手）のキャプチャ。ScreenCaptureKit の
/// `SCStream`（音声有効）で受け、ファイルへ書き出す
/// （docs/P3計画.md の F1 第一候補）。
/// 映像は最小サイズに絞り、映像サンプルは破棄する（音声のみ録る）。
/// 権限（TCC: Screen Recording）の付与は本人の手動操作が前提で、
/// このクラスは付与済みであることを期待する。
///
/// 2026-11-09 の本番面談で system トラックが全編無音（−91 dB）になった
/// 欠陥への対策（Issue #11）:
/// - 映像出力も登録する。旧実装は音声出力だけを登録しており、
///   音声バッファが無音で流れる事象の切り分け対象になった。
///   設計（P3計画）どおり「映像サンプルは破棄する」形に戻す。
/// - CMSampleBuffer からのコピーは、受け側リストをチャンネル数ぶん
///   確保し、PCM バッファ自身のストレージへ明示コピーする
///   （旧実装は 1 バッファ分のサイズ指定＋ retained block buffer の
///   寿命に依存していた）。
/// - 診断として、生バッファ（コピー元）のピークレベルと
///   コピー後のピークレベル、ASBD、バッファ数をセッション
///   フォルダの capture-diag.json に残す。再発時に「キャプチャ側が
///   無音か、変換側で消えたか」を即断できるようにするため。
final class SystemAudioCapture: NSObject {
    private var stream: SCStream?
    private var audioFile: AVAudioFile?
    private var converter: AudioFormatConverter?
    private(set) var isCapturing = false

    /// レベル（dBFS）の通知。無音検知に使う。
    var onLevel: ((Float) -> Void)?
    /// エラーの通知（失敗を黙って成功扱いにしない）。
    var onError: ((Error) -> Void)?

    // 診断（capture-diag.json）の蓄積値。サンプル処理キューから
    // 更新し、stop 時に書き出す。診断専用なので厳密な同期はしない。
    private var diagStartedAt: Date?
    private var diagASBD = ""
    private var diagBufferCount = 0
    private var diagRawPeakDb: Float = -160
    private var diagPcmPeakDb: Float = -160
    private var diagEarlyWritten = false

    func start(fileURL: URL) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false
        )
        guard let display = content.displays.first else {
            throw CaptureError.noDisplay
        }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.sampleRate = 48_000
        configuration.channelCount = 1
        // 映像は使わない。最小サイズと低い頻度に絞り、サンプルは破棄する。
        configuration.width = 64
        configuration.height = 64
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 5

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(
            self, type: .screen,
            sampleHandlerQueue: DispatchQueue(label: "system-video-discard")
        )
        try stream.addStreamOutput(
            self, type: .audio,
            sampleHandlerQueue: DispatchQueue(label: "system-audio-capture")
        )
        self.stream = stream
        self.pendingFileURL = fileURL
        diagStartedAt = Date()
        diagASBD = ""
        diagBufferCount = 0
        diagRawPeakDb = -160
        diagPcmPeakDb = -160
        diagEarlyWritten = false
        try await stream.startCapture()
        isCapturing = true
    }

    func stop() async {
        if isCapturing {
            try? await stream?.stopCapture()
        }
        isCapturing = false
        writeDiag()
        stream = nil
        audioFile = nil
        converter = nil
    }

    private var pendingFileURL: URL?

    /// 最初の音声サンプルでフォーマットを確定し、書き出しファイルを開く。
    /// ファイル自体は 48kHz・モノラル統一（AudioFileWriter）で開き、
    /// サンプルの形式から書き出し形式への変換器もここで作る。
    private func openFileIfNeeded(format: AVAudioFormat) throws {
        guard audioFile == nil, let url = pendingFileURL else { return }
        let file = try AudioFileWriter.openFile(url: url)
        guard
            let formatConverter = AudioFormatConverter(
                from: format, to: file.processingFormat)
        else {
            throw AudioWriteError.converterUnavailable
        }
        audioFile = file
        converter = formatConverter
    }

    /// CMSampleBuffer（音声）を AVAudioPCMBuffer へ移す。
    /// 受け側の AudioBufferList はフォーマットのチャンネル数ぶんを
    /// 確保し（インターリーブでない形式でも全バッファを受けられる
    /// ように）、サンプルデータは PCM バッファ自身のストレージへ
    /// 明示的にコピーする。あわせて診断用に、コピー元の生データから
    /// ピークレベル（dBFS）を測る。
    private func pcmBuffer(from sampleBuffer: CMSampleBuffer) -> AVAudioPCMBuffer? {
        guard let description = CMSampleBufferGetFormatDescription(sampleBuffer)
        else { return nil }
        guard
            let asbdPointer =
                CMAudioFormatDescriptionGetStreamBasicDescription(description)
        else { return nil }
        let asbd = asbdPointer.pointee
        let format = AVAudioFormat(cmAudioFormatDescription: description)
        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount)
        else { return nil }
        buffer.frameLength = frameCount

        let channelCount = max(Int(asbd.mChannelsPerFrame), 1)
        let listSize =
            MemoryLayout<AudioBufferList>.size
            + MemoryLayout<AudioBuffer>.size * (channelCount - 1)
        let rawList = UnsafeMutableRawPointer.allocate(
            byteCount: listSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { rawList.deallocate() }
        let listPointer = rawList.assumingMemoryBound(to: AudioBufferList.self)
        var blockBuffer: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: listPointer,
            bufferListSize: listSize,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr else { return nil }

        let source = UnsafeMutableAudioBufferListPointer(listPointer)
        let destination = UnsafeMutableAudioBufferListPointer(
            buffer.mutableAudioBufferList)
        guard source.count == destination.count else { return nil }
        for index in 0..<source.count {
            let sourceBuffer = source[index]
            let destinationBuffer = destination[index]
            guard let sourceData = sourceBuffer.mData,
                  let destinationData = destinationBuffer.mData
            else { return nil }
            let byteCount = min(
                Int(sourceBuffer.mDataByteSize),
                Int(destinationBuffer.mDataByteSize)
            )
            if byteCount > 0 {
                memcpy(destinationData, sourceData, byteCount)
                updateRawPeak(data: sourceData, byteCount: byteCount, asbd: asbd)
            }
        }
        if diagASBD.isEmpty {
            diagASBD =
                "rate=\(asbd.mSampleRate) channels=\(asbd.mChannelsPerFrame) "
                + "formatID=\(asbd.mFormatID) flags=\(asbd.mFormatFlags) "
                + "bytesPerFrame=\(asbd.mBytesPerFrame) "
                + "buffers=\(source.count)"
        }
        return buffer
    }

    /// コピー元の生 PCM からピークレベル（dBFS）を測り、最大値を残す。
    /// Float32 と Int16 のみ対応し、それ以外は測らない。
    private func updateRawPeak(
        data: UnsafeMutableRawPointer, byteCount: Int,
        asbd: AudioStreamBasicDescription
    ) {
        let isFloat = asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0
        let isSignedInt = asbd.mFormatFlags & kAudioFormatFlagIsSignedInteger != 0
        var peak: Float = 0
        if isFloat {
            let count = byteCount / MemoryLayout<Float>.size
            let samples = data.bindMemory(to: Float.self, capacity: count)
            for index in 0..<count {
                peak = max(peak, abs(samples[index]))
            }
        } else if isSignedInt {
            let count = byteCount / MemoryLayout<Int16>.size
            let samples = data.bindMemory(to: Int16.self, capacity: count)
            for index in 0..<count {
                peak = max(peak, abs(Float(samples[index]) / 32768.0))
            }
        } else {
            return
        }
        if peak > 0 {
            diagRawPeakDb = max(diagRawPeakDb, 20 * log10(peak))
        }
    }

    /// 診断をセッションフォルダ（音声ファイルと同じ場所）の
    /// capture-diag.json に書く。rawPeakDb が健全で pcmPeakDb が
    /// 無音なら変換側、両方無音ならキャプチャ側の問題と切り分ける。
    private func writeDiag() {
        guard let fileURL = pendingFileURL else { return }
        let diagURL =
            fileURL
            .deletingLastPathComponent()
            .appendingPathComponent("capture-diag.json")
        let formatter = ISO8601DateFormatter()
        let record: [String: Any] = [
            "startedAt": diagStartedAt.map { formatter.string(from: $0) } ?? "",
            "stoppedAt": formatter.string(from: Date()),
            "asbd": diagASBD,
            "buffers": diagBufferCount,
            "rawPeakDb": Double(diagRawPeakDb),
            "pcmPeakDb": Double(diagPcmPeakDb),
            "screenOutputRegistered": true,
        ]
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: record, options: [.prettyPrinted, .sortedKeys])
        else { return }
        try? data.write(to: diagURL, options: [.atomic])
    }
}

extension SystemAudioCapture: SCStreamOutput {
    func stream(
        _ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        // 映像サンプルは受け取るだけで破棄する（音声のみ録る設計）。
        guard type == .audio, sampleBuffer.isValid else { return }
        guard let buffer = pcmBuffer(from: sampleBuffer) else { return }
        diagBufferCount += 1
        diagPcmPeakDb = max(diagPcmPeakDb, AudioLevel.rmsDb(of: buffer))
        if !diagEarlyWritten {
            diagEarlyWritten = true
            writeDiag()
        }
        do {
            try openFileIfNeeded(format: buffer.format)
            if let converted = try converter?.convert(buffer) {
                try audioFile?.write(from: converted)
            }
        } catch {
            onError?(error)
        }
        onLevel?(AudioLevel.rmsDb(of: buffer))
    }
}

extension SystemAudioCapture: SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        isCapturing = false
        onError?(error)
    }
}

enum CaptureError: Error {
    case noDisplay
}
