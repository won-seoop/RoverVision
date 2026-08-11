import SwiftUI

struct ContentView: View {
    @StateObject private var probe = CameraProbe()
    @State private var macHost = "192.168.0.36"

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    statusCard

                    VStack(alignment: .leading, spacing: 8) {
                        Text("실시간 프레임")
                            .font(.headline)

                        HStack(spacing: 12) {
                            frameCard(title: "Wide", fps: probe.wideFPS, total: probe.wideFrames)
                            frameCard(title: "Ultra Wide", fps: probe.ultraFPS, total: probe.ultraFrames)
                        }
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        Text("Mac으로 무선 전송")
                            .font(.headline)
                        TextField("Mac IP 주소", text: $macHost)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.numbersAndPunctuation)
                            .textFieldStyle(.roundedBorder)
                        HStack {
                            Button("연결 및 전송") {
                                probe.connectToMac(host: macHost)
                            }
                            .buttonStyle(.borderedProminent)
                            Button("연결 해제") {
                                probe.disconnectFromMac()
                            }
                            .buttonStyle(.bordered)
                        }
                        Text(probe.streamStatus)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    .padding(12)
                    .background(Color.blue.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))

                    VStack(alignment: .leading, spacing: 8) {
                        Text("기기 검사 결과")
                            .font(.headline)

                        Text(probe.report)
                            .font(.system(.footnote, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
                    }

                    Text("판정 기준: 지원 조합에 Wide + Ultra Wide가 있고, 두 프레임 숫자가 모두 계속 증가하면 직접 스테레오 비전 실험이 가능합니다.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                .padding()
            }
            .navigationTitle("MultiCam Probe")
            .task {
                probe.start()
            }
            .onDisappear {
                probe.stop()
            }
        }
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("현재 상태")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(probe.status)
                .font(.headline)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(statusColor.opacity(0.15), in: RoundedRectangle(cornerRadius: 14))
    }

    private var statusColor: Color {
        if probe.status.contains("성공") { return .green }
        if probe.status.contains("실패") || probe.status.contains("없습니다") { return .red }
        return .orange
    }

    private func frameCard(title: String, fps: Double, total: Int) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Text(String(format: "%.1f fps", fps))
                .font(.title3.bold())
            Text("총 \(total) 프레임")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
    }
}
