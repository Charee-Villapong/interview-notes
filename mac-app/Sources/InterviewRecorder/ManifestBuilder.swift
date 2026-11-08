import CryptoKit
import Foundation

/// 転送マニフェストの生成。形式は VM 側 P1 の `Manifest`
/// （pipeline の manifest.py）と同一:
/// `{"session_id": ..., "files": [{"name", "size", "sha256"}]}`。
/// 配置はセッションフォルダ直下の `manifest.json`。
enum ManifestBuilder {
    struct FileEntry: Codable {
        var name: String
        var size: Int
        var sha256: String
    }

    struct Manifest: Codable {
        var session_id: String
        var files: [FileEntry]
    }

    static func build(sessionId: String, fileURLs: [URL]) throws -> Manifest {
        var entries: [FileEntry] = []
        for url in fileURLs {
            let size = try FileManager.default
                .attributesOfItem(atPath: url.path)[.size] as? Int ?? 0
            entries.append(
                FileEntry(
                    name: url.lastPathComponent,
                    size: size,
                    sha256: try sha256(of: url)
                )
            )
        }
        return Manifest(session_id: sessionId, files: entries)
    }

    static func write(_ manifest: Manifest, to url: URL) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(manifest)
        try data.write(to: url, options: [.atomic])
    }

    /// SHA-256 を 1MB 単位で読みながら計算する（VM 側の検証と同方式）。
    static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let chunk = try handle.read(upToCount: 1024 * 1024) ?? Data()
            if chunk.isEmpty { break }
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
