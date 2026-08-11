# MultiCamProbe

iPhone의 후면 Wide 및 Ultra Wide 카메라를 동시에 사용할 수 있는지 실기기에서 검사하는 최소 앱입니다.

## VS Code + Xcode 도구로 실행

1. VS Code에서 이 폴더를 열어 Swift 코드를 수정합니다.
2. iPhone 12 mini를 USB로 연결하고 잠금을 해제합니다.
3. 최초 한 번은 `MultiCamProbe.xcodeproj`를 Xcode로 엽니다.
4. `Signing & Capabilities`에서 본인의 Apple ID Team을 선택합니다.
5. 실행 대상을 연결된 iPhone으로 고르고 Run을 누릅니다.
6. 카메라 권한을 허용하고 Wide/Ultra Wide 프레임 수가 모두 증가하는지 확인합니다.

시뮬레이터에서는 실제 카메라 조합을 판정할 수 없습니다.

## 같은 Wi-Fi로 Mac에 프레임 전송

1. Mac에서 `python3 mac_receiver.py`를 실행합니다.
2. 브라우저에서 `http://127.0.0.1:8080`을 엽니다.
3. iPhone 앱의 Mac IP에 Mac의 Wi-Fi 주소를 입력합니다.
4. `연결 및 전송`을 누르고 로컬 네트워크 권한을 허용합니다.

현재 프로토타입은 같은 시점으로 묶은 두 프레임을 640px JPEG로 약 6쌍/초 전송합니다.

## Mac 화면으로 실시간 캘리브레이션

Mac 내장 화면을 사용할 때:

```bash
.venv/bin/python live_calibrate.py
```

전체 화면 체크무늬가 나타나면 iPhone 후면 카메라 두 개가 모두 화면을 보도록 들고 천천히 좌우, 위아래, 앞뒤로 움직입니다. 서로 다른 시점 25쌍이 자동 저장되면 `calibration/stereo_calibration.npz`가 생성됩니다.

## 실시간 스테레오 거리 계산

수신기와 iPhone 무선 연결이 켜진 상태에서:

```bash
.venv/bin/python live_depth.py
```

왼쪽에는 보정된 Wide 영상, 오른쪽에는 Depth Map이 표시됩니다. 중앙 사각형의 대표 거리가 m 단위로 표시되며 빨간색은 가깝고 파란색은 먼 영역입니다. `S`는 현재 결과 저장, `Q` 또는 `ESC`는 종료입니다.

OpenCV 창이 보이지 않는 경우 웹 화면을 사용할 수 있습니다.

```bash
.venv/bin/python depth_web.py
```

Safari에서 `http://127.0.0.1:8081`을 열면 실시간 거리 화면이 표시됩니다.

중앙 영역에서 계산한 거리가 0.60m 이하면 빨간색 `OBSTACLE`, 더 멀면
초록색 `CLEAR`로 표시합니다. 유효한 거리 픽셀이 부족하면 주황색
`UNKNOWN`으로 표시합니다.
