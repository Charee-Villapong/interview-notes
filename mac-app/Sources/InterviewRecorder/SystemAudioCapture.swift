import AVFoundation
import CoreMedia
import ScreenCaptureKit

/// システム音声（相手）のキャプチャ。ScreenCaptureKit の
/// `SCStream`（音声有効）で受け、ファイルへ書き出す
/// （docs/P3計画.md の F1 第一候補）。
/// 映像は最小サイズに絞り、映像サンプルは破棄する（音声のみ録る）。
/// 権限（TCC: Screen Recording）の付与は本人の手動操作が前提で、
/// このクラスは付与済みであることを期待する。
final class SystemAudioCapture: NSObject {
    private var stream: SCStream?
    private var audioFile: AVAudioFile?
    private var converter: AudioFormatConverter?
    private(set) var isCapturing = false

    /// レベル（dBFS）の通知。無音検知に使う。
    var onLevel: ((Float) -> Void)?
    /// エラーの通知（失敗を黙って成功扱いにしない）。
    var onError: ((Error) -> Void)?

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
        // 映像は使わない。最小サイズと低い頻度に絞る。
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 5

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(
            self, type: .audio,
            sampleHandlerQueue: DispatchQueue(label: "system-audio-capture")
        )
        self.stream = stream
        self.pendingFileURL = fileURL
        try await stream.startCapture()
        isCapturing = true
    }

    func stop() async {
        if isCapturing {
            try? await stream?.stopCapture()
        }
        isCapturing = false
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
    /// TODO(F1): インターリーブ有無とチャンネル配置の実挙動は
    /// Mac 実働段階で実測し、この変換を確定する。
    private func pcmBuffer(from sampleBuffer: CMSampleBuffer) -> AVAudioPCMBuffer? {
        guard let description = CMSampleBufferGetFormatDescription(sampleBuffer)
        else { return nil }
        let format = AVAudioFormat(cmAudioFormatDescription: description)
        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount)
        else { return nil }
        buffer.frameLength = frameCount
        var blockBuffer: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: buffer.mutableAudioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: &blockBuffer
        )
        return status == noErr ? buffer : nil
    }
}

extension SystemAudioCapture: SCStreamOutput {
    func stream(
        _ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio, sampleBuffer.isValid else { return }
        guard let buffer = pcmBuffer(from: sampleBuffer) else { return }
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
