import AVFoundation
import CoreImage
import CoreMedia
import CoreVideo
import Foundation
import UIKit

final class CameraProbe: NSObject, ObservableObject {
    @Published private(set) var status = "카메라 권한 확인 중"
    @Published private(set) var report = "실제 iPhone에서 검사합니다. 시뮬레이터에서는 카메라 조합을 확인할 수 없습니다."
    @Published private(set) var wideFPS = 0.0
    @Published private(set) var ultraFPS = 0.0
    @Published private(set) var wideFrames = 0
    @Published private(set) var ultraFrames = 0
    @Published private(set) var streamStatus = "무선 연결 안 됨"

    private let sessionQueue = DispatchQueue(label: "multicam.probe.session")
    private let outputQueue = DispatchQueue(label: "multicam.probe.output")

    private var session: AVCaptureMultiCamSession?
    private var wideOutput: AVCaptureVideoDataOutput?
    private var ultraOutput: AVCaptureVideoDataOutput?
    private let streamer = NetworkStreamer()
    private let imageContext = CIContext(options: [.cacheIntermediates: false])
    private var wideWindowFrames = 0
    private var ultraWindowFrames = 0
    private var totalWideFrames = 0
    private var totalUltraFrames = 0
    private var windowStartedAt = CFAbsoluteTimeGetCurrent()
    private var synchronizedPairSequence: UInt64 = 0
    private var latestWideSample: CMSampleBuffer?
    private var latestUltraSample: CMSampleBuffer?

    override init() {
        super.init()
        streamer.onStatusChange = { [weak self] message in
            DispatchQueue.main.async {
                self?.streamStatus = message
            }
        }
    }

    func connectToMac(host: String) {
        streamer.connect(host: host, port: 5050)
    }

    func disconnectFromMac() {
        streamer.disconnect()
    }

    func start() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            configureAndStart()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                guard let self else { return }
                if granted {
                    self.configureAndStart()
                } else {
                    self.publish(status: "실패: 카메라 권한이 거부됨", report: "설정 > 개인정보 보호 및 보안 > 카메라에서 권한을 허용하세요.")
                }
            }
        default:
            publish(status: "실패: 카메라 권한이 없음", report: "설정 > 개인정보 보호 및 보안 > 카메라에서 권한을 허용하세요.")
        }
    }

    func stop() {
        sessionQueue.async { [weak self] in
            self?.session?.stopRunning()
            self?.session = nil
        }
        streamer.disconnect()
    }

    private func configureAndStart() {
        sessionQueue.async { [weak self] in
            self?.configureOnSessionQueue()
        }
    }

    private func configureOnSessionQueue() {
        guard AVCaptureMultiCamSession.isMultiCamSupported else {
            publish(
                status: "실패: MultiCam을 지원하지 않음",
                report: "AVCaptureMultiCamSession.isMultiCamSupported = false\n실제 iPhone 12 mini에서 실행했는지 확인하세요."
            )
            return
        }

        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: [
                .builtInWideAngleCamera,
                .builtInUltraWideCamera,
                .builtInTelephotoCamera,
                .builtInTrueDepthCamera
            ],
            mediaType: .video,
            position: .unspecified
        )

        let devices = discovery.devices
        let sets = discovery.supportedMultiCamDeviceSets
        let wide = devices.first { $0.position == .back && $0.deviceType == .builtInWideAngleCamera }
        let ultra = devices.first { $0.position == .back && $0.deviceType == .builtInUltraWideCamera }

        var lines = [
            "MultiCam 지원: YES",
            "발견한 카메라:",
            devices.map { "- \(friendlyName(for: $0)) [\($0.uniqueID)]" }.joined(separator: "\n"),
            "",
            "동시 사용 지원 조합:"
        ]

        if sets.isEmpty {
            lines.append("- 없음")
        } else {
            for (index, set) in sets.enumerated() {
                let names = set.map(friendlyName(for:)).sorted().joined(separator: " + ")
                lines.append("\(index + 1). \(names)")
            }
        }

        guard let wide, let ultra else {
            lines.append("\n판정: 후면 Wide 또는 Ultra Wide를 찾지 못했습니다.")
            publish(status: "실패: 대상 카메라가 없음", report: lines.joined(separator: "\n"))
            return
        }

        let targetPairIsSupported = sets.contains { set in
            set.contains(where: { $0.uniqueID == wide.uniqueID }) &&
            set.contains(where: { $0.uniqueID == ultra.uniqueID })
        }

        guard targetPairIsSupported else {
            lines.append("\n판정: Wide + Ultra Wide 조합이 지원 목록에 없습니다.")
            publish(status: "실패: Wide + Ultra Wide 조합 없음", report: lines.joined(separator: "\n"))
            return
        }

        do {
            let wideFormat = try selectMultiCamFormat(for: wide)
            let ultraFormat = try selectMultiCamFormat(for: ultra)
            lines.append("\n선택 포맷:")
            lines.append("- Wide: \(description(of: wideFormat))")
            lines.append("- Ultra Wide: \(description(of: ultraFormat))")

            let newSession = AVCaptureMultiCamSession()
            let newWideOutput = AVCaptureVideoDataOutput()
            let newUltraOutput = AVCaptureVideoDataOutput()

            newWideOutput.alwaysDiscardsLateVideoFrames = true
            newUltraOutput.alwaysDiscardsLateVideoFrames = true
            let videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA)
            ]
            newWideOutput.videoSettings = videoSettings
            newUltraOutput.videoSettings = videoSettings
            newWideOutput.setSampleBufferDelegate(self, queue: outputQueue)
            newUltraOutput.setSampleBufferDelegate(self, queue: outputQueue)
            newSession.beginConfiguration()
            do {
                try attach(device: wide, output: newWideOutput, to: newSession)
                try attach(device: ultra, output: newUltraOutput, to: newSession)
            } catch {
                newSession.commitConfiguration()
                throw error
            }
            newSession.commitConfiguration()

            session = newSession
            wideOutput = newWideOutput
            ultraOutput = newUltraOutput
            resetCounters()

            lines.append("\n판정: 조합과 세션 구성이 성공했습니다. 프레임 수신을 확인 중입니다.")
            publish(status: "세션 구성 성공, 프레임 대기 중", report: lines.joined(separator: "\n"))

            newSession.startRunning()
            monitorInitialFrames(reportLines: lines)
        } catch {
            lines.append("\n세션 구성 오류: \(error.localizedDescription)")
            publish(status: "실패: 세션 구성 오류", report: lines.joined(separator: "\n"))
        }
    }

    private func selectMultiCamFormat(for device: AVCaptureDevice) throws -> AVCaptureDevice.Format {
        let supported = device.formats.filter(\.isMultiCamSupported)
        guard !supported.isEmpty else {
            throw ProbeError.noMultiCamFormat(friendlyName(for: device))
        }

        let preferred = supported.first { format in
            let dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            return dimensions.width == 1280 && dimensions.height == 720 && supports30FPS(format)
        } ?? supported.first { supports30FPS($0) } ?? supported[0]

        try device.lockForConfiguration()
        device.activeFormat = preferred
        if supports30FPS(preferred) {
            device.activeVideoMinFrameDuration = CMTime(value: 1, timescale: 30)
            device.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: 30)
        }
        device.unlockForConfiguration()
        return preferred
    }

    private func supports30FPS(_ format: AVCaptureDevice.Format) -> Bool {
        format.videoSupportedFrameRateRanges.contains { $0.minFrameRate <= 30 && $0.maxFrameRate >= 30 }
    }

    private func attach(
        device: AVCaptureDevice,
        output: AVCaptureVideoDataOutput,
        to session: AVCaptureMultiCamSession
    ) throws {
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else {
            throw ProbeError.cannotAddInput(friendlyName(for: device))
        }
        session.addInputWithNoConnections(input)

        guard session.canAddOutput(output) else {
            throw ProbeError.cannotAddOutput(friendlyName(for: device))
        }
        session.addOutputWithNoConnections(output)

        guard let port = input.ports.first(where: { $0.mediaType == .video }) else {
            throw ProbeError.noVideoPort(friendlyName(for: device))
        }

        let connection = AVCaptureConnection(inputPorts: [port], output: output)
        guard session.canAddConnection(connection) else {
            throw ProbeError.cannotAddConnection(friendlyName(for: device))
        }
        session.addConnection(connection)
    }

    private func monitorInitialFrames(reportLines: [String]) {
        outputQueue.asyncAfter(deadline: .now() + 3) { [weak self] in
            guard let self else { return }
            let bothReceiving = self.totalWideFrames > 0 && self.totalUltraFrames > 0
            var lines = reportLines
            if bothReceiving {
                lines.append("\n최종 판정: 두 카메라 모두 실제 프레임을 수신했습니다.")
                self.publish(status: "성공: 두 카메라 프레임 수신 중", report: lines.joined(separator: "\n"))
            } else {
                lines.append("\n최종 판정: 3초 안에 두 출력 모두에서 프레임을 받지 못했습니다.")
                self.publish(status: "실패: 프레임 수신 불완전", report: lines.joined(separator: "\n"))
            }
        }
    }

    private func resetCounters() {
        outputQueue.async { [weak self] in
            self?.wideWindowFrames = 0
            self?.ultraWindowFrames = 0
            self?.totalWideFrames = 0
            self?.totalUltraFrames = 0
            self?.synchronizedPairSequence = 0
            self?.latestWideSample = nil
            self?.latestUltraSample = nil
            self?.windowStartedAt = CFAbsoluteTimeGetCurrent()
        }
    }

    private func friendlyName(for device: AVCaptureDevice) -> String {
        let position = device.position == .back ? "Back" : device.position == .front ? "Front" : "Unspecified"
        let type: String
        switch device.deviceType {
        case .builtInWideAngleCamera: type = "Wide"
        case .builtInUltraWideCamera: type = "Ultra Wide"
        case .builtInTelephotoCamera: type = "Telephoto"
        case .builtInTrueDepthCamera: type = "TrueDepth"
        default: type = device.localizedName
        }
        return "\(position) \(type)"
    }

    private func description(of format: AVCaptureDevice.Format) -> String {
        let dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
        return "\(dimensions.width)x\(dimensions.height), MultiCam=YES"
    }

    private func publish(status: String, report: String) {
        DispatchQueue.main.async { [weak self] in
            self?.status = status
            self?.report = report
        }
    }
}

extension CameraProbe: AVCaptureVideoDataOutputSampleBufferDelegate {
    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        if output === wideOutput {
            wideWindowFrames += 1
            totalWideFrames += 1
            latestWideSample = sampleBuffer
        } else if output === ultraOutput {
            ultraWindowFrames += 1
            totalUltraFrames += 1
            latestUltraSample = sampleBuffer
        }

        matchAndSendClosestPair()

        let now = CFAbsoluteTimeGetCurrent()
        let elapsed = now - windowStartedAt
        guard elapsed >= 1 else { return }

        let currentWideFPS = Double(wideWindowFrames) / elapsed
        let currentUltraFPS = Double(ultraWindowFrames) / elapsed
        let currentWideTotal = totalWideFrames
        let currentUltraTotal = totalUltraFrames

        wideWindowFrames = 0
        ultraWindowFrames = 0
        windowStartedAt = now

        DispatchQueue.main.async { [weak self] in
            self?.wideFPS = currentWideFPS
            self?.ultraFPS = currentUltraFPS
            self?.wideFrames = currentWideTotal
            self?.ultraFrames = currentUltraTotal
        }
    }

    private func matchAndSendClosestPair() {
        guard let wideSample = latestWideSample, let ultraSample = latestUltraSample else { return }
        let wideTimestamp = CMSampleBufferGetPresentationTimeStamp(wideSample)
        let ultraTimestamp = CMSampleBufferGetPresentationTimeStamp(ultraSample)
        let difference = CMTimeGetSeconds(CMTimeSubtract(wideTimestamp, ultraTimestamp))

        // 30fps에서 한 프레임 이내인 두 샘플만 한 쌍으로 취급합니다.
        guard abs(difference) <= (1.0 / 30.0) else {
            if difference < 0 {
                latestWideSample = nil
            } else {
                latestUltraSample = nil
            }
            return
        }

        latestWideSample = nil
        latestUltraSample = nil
        synchronizedPairSequence += 1

        guard synchronizedPairSequence.isMultiple(of: 5), streamer.isReady,
              let wideJPEG = makeJPEG(from: wideSample),
              let ultraJPEG = makeJPEG(from: ultraSample) else { return }

        streamer.sendPair(
            sequence: synchronizedPairSequence,
            timestampMicroseconds: UInt64(max(0, CMTimeGetSeconds(wideTimestamp) * 1_000_000)),
            wideJPEG: wideJPEG,
            ultraJPEG: ultraJPEG
        )
    }

    private func makeJPEG(from sampleBuffer: CMSampleBuffer) -> Data? {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return nil }
        var image = CIImage(cvPixelBuffer: pixelBuffer)
        let width = image.extent.width
        if width > 640 {
            let scale = 640 / width
            image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        }
        guard let cgImage = imageContext.createCGImage(image, from: image.extent) else { return nil }
        return UIImage(cgImage: cgImage).jpegData(compressionQuality: 0.55)
    }
}

private enum ProbeError: LocalizedError {
    case noMultiCamFormat(String)
    case cannotAddInput(String)
    case cannotAddOutput(String)
    case noVideoPort(String)
    case cannotAddConnection(String)

    var errorDescription: String? {
        switch self {
        case .noMultiCamFormat(let name): return "\(name)에 MultiCam 지원 포맷이 없습니다."
        case .cannotAddInput(let name): return "\(name) 입력을 추가할 수 없습니다."
        case .cannotAddOutput(let name): return "\(name) 출력을 추가할 수 없습니다."
        case .noVideoPort(let name): return "\(name) 비디오 포트를 찾지 못했습니다."
        case .cannotAddConnection(let name): return "\(name) 연결을 추가할 수 없습니다."
        }
    }
}
