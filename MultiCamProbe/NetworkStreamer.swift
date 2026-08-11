import Foundation
import Network

final class NetworkStreamer {
    var onStatusChange: ((String) -> Void)?
    private(set) var isReady = false

    private let queue = DispatchQueue(label: "multicam.probe.network")
    private var connection: NWConnection?

    func connect(host: String, port: UInt16) {
        disconnect()

        guard !host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let networkPort = NWEndpoint.Port(rawValue: port) else {
            onStatusChange?("연결 실패: Mac 주소를 확인하세요")
            return
        }

        let newConnection = NWConnection(
            host: NWEndpoint.Host(host),
            port: networkPort,
            using: .tcp
        )
        connection = newConnection
        onStatusChange?("Mac에 연결 중: \(host):\(port)")

        newConnection.stateUpdateHandler = { [weak self, weak newConnection] state in
            guard let self, self.connection === newConnection else { return }
            switch state {
            case .ready:
                self.isReady = true
                self.onStatusChange?("연결 성공: 프레임 전송 중")
            case .waiting(let error):
                self.isReady = false
                self.onStatusChange?("연결 대기: \(error.localizedDescription)")
            case .failed(let error):
                self.isReady = false
                self.onStatusChange?("연결 실패: \(error.localizedDescription)")
            case .cancelled:
                self.isReady = false
                self.onStatusChange?("무선 연결 안 됨")
            default:
                break
            }
        }
        newConnection.start(queue: queue)
    }

    func disconnect() {
        connection?.cancel()
        connection = nil
        isReady = false
        onStatusChange?("무선 연결 안 됨")
    }

    func sendPair(
        sequence: UInt64,
        timestampMicroseconds: UInt64,
        wideJPEG: Data,
        ultraJPEG: Data
    ) {
        guard isReady, let connection else { return }

        var packet = Data(capacity: 32 + wideJPEG.count + ultraJPEG.count)
        packet.append(contentsOf: [0x4D, 0x43, 0x41, 0x4D]) // MCAM
        packet.appendBigEndian(UInt32(1))
        packet.appendBigEndian(sequence)
        packet.appendBigEndian(timestampMicroseconds)
        packet.appendBigEndian(UInt32(wideJPEG.count))
        packet.appendBigEndian(UInt32(ultraJPEG.count))
        packet.append(wideJPEG)
        packet.append(ultraJPEG)

        connection.send(content: packet, completion: .contentProcessed { [weak self] error in
            if let error {
                self?.isReady = false
                self?.onStatusChange?("전송 실패: \(error.localizedDescription)")
            }
        })
    }
}

private extension Data {
    mutating func appendBigEndian<T: FixedWidthInteger>(_ integer: T) {
        var value = integer.bigEndian
        Swift.withUnsafeBytes(of: &value) { bytes in
            append(contentsOf: bytes)
        }
    }
}
