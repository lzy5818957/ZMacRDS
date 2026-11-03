import Foundation

struct Problem: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}

struct Sample: Decodable {
    let source, runId, osBuild: String
    let bootId: UUID
    let uid: UInt32
    let guiUid, consoleUid: UInt32?
    let auditSessionId: Int32
    let uptimeMs: UInt64
    let hasGuiSession, screenCapturePreflight, accessibilityTrusted: Bool
    let onConsole, loginComplete: Bool?
}

struct Report {
    let first: Sample
    let samples: [Sample]

    init(_ path: String) throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        var rows: [Sample] = []
        for (index, line) in data.split(separator: 10).enumerated() {
            let location = "\(path), line \(index + 1)"
            let row: Sample
            do {
                row = try decoder.decode(Sample.self, from: Data(line))
            } catch {
                throw Problem("\(location): invalid M0 sample (\(error))")
            }
            guard row.source == "m0-launchagent", !row.runId.isEmpty, !row.osBuild.isEmpty,
                  row.hasGuiSession, row.loginComplete == true, row.uid > 0,
                  row.guiUid == row.uid, row.auditSessionId > 0 else {
                throw Problem("\(location): expected a logged-in agent with matching process/GUI UIDs and a valid audit session")
            }
            if let prev = rows.last {
                guard row.uid == prev.uid, row.auditSessionId == prev.auditSessionId,
                      row.bootId == prev.bootId, row.runId == prev.runId,
                      row.osBuild == prev.osBuild, row.uptimeMs > prev.uptimeMs else {
                    throw Problem("\(location): session, run or boot changed, or time did not advance")
                }
            }
            rows.append(row)
        }
        guard let first = rows.first else { throw Problem("\(path): empty log") }
        self.first = first
        self.samples = rows
    }

    // Only consecutive samples can establish a stable interval. Missing Quartz
    // flags are acceptable when SystemConfiguration names the actual console UID.
    func intervals(console: UInt32) -> [ClosedRange<UInt64>] {
        var spans: [ClosedRange<UInt64>] = []
        var previousMatched = false
        for row in samples {
            let isLocal = console == row.uid
            let matched = row.consoleUid == console && (row.onConsole == nil || row.onConsole == isLocal)
            if matched {
                if previousMatched, let last = spans.last, row.uptimeMs - last.upperBound <= 5_000 {
                    spans[spans.count - 1] = last.lowerBound...row.uptimeMs
                } else {
                    spans.append(row.uptimeMs...row.uptimeMs)
                }
            }
            previousMatched = matched
        }
        return spans
    }

    func describe(_ side: String) {
        let capture = samples.filter { $0.screenCapturePreflight }.count
        let access = samples.filter { $0.accessibilityTrusted }.count
        print("\(side): uid=\(first.uid) audit_session=\(first.auditSessionId) samples=\(samples.count) screen_capture_granted=\(capture)/\(samples.count) accessibility_granted=\(access)/\(samples.count)")
    }
}

func verify(_ a: Report, _ b: Report) throws {
    a.describe("A")
    b.describe("B")
    guard a.first.runId == b.first.runId, a.first.bootId == b.first.bootId,
          a.first.osBuild == b.first.osBuild else {
        throw Problem("logs must come from the same run and macOS boot")
    }
    guard a.first.uid != b.first.uid, a.first.auditSessionId != b.first.auditSessionId else {
        throw Problem("the two agents must have distinct UIDs and distinct audit sessions")
    }
    for (side, console) in [("A", a.first.uid), ("B", b.first.uid)] {
        let aSpans = a.intervals(console: console)
        let bSpans = b.intervals(console: console)
        var longest: UInt64 = 0
        for left in aSpans {
            for right in bSpans where left.overlaps(right) {
                longest = max(longest, min(left.upperBound, right.upperBound) - max(left.lowerBound, right.lowerBound))
            }
        }
        print("\(side) on console: simultaneous observations span \(longest) ms")
        guard longest >= 4_000 else {
            throw Problem("need 4 s of overlapping samples with \(side) on console; keep both accounts logged in and switch to each for 15 s")
        }
    }
    print("PASS M0: run=\(a.first.runId) macOS_build=\(a.first.osBuild)")
}

guard CommandLine.arguments.count == 3 else {
    FileHandle.standardError.write(Data("Usage: m0 verify A.jsonl B.jsonl\n".utf8))
    exit(2)
}
do {
    try verify(Report(CommandLine.arguments[1]), Report(CommandLine.arguments[2]))
} catch {
    FileHandle.standardError.write(Data("FAIL M0: \(error.localizedDescription)\n".utf8))
    exit(1)
}
