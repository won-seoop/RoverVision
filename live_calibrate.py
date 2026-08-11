#!/usr/bin/env python3
import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np

PATTERN_SIZE = (9, 6)  # 내부 모서리 수. 실제 칸은 10 x 7입니다.
PACKET_HEADER = struct.Struct("!4sIQQII")
DEFAULT_SCREEN_WIDTH_MM = 326.75376393687185
WINDOW_NAME = "RoverVision Stereo Calibration"


def parse_args():
    parser = argparse.ArgumentParser(description="Live stereo calibration using the Mac display")
    parser.add_argument("--receiver", default="http://127.0.0.1:8080")
    parser.add_argument("--target", type=int, default=25)
    parser.add_argument("--screen-width-mm", type=float, default=DEFAULT_SCREEN_WIDTH_MM)
    parser.add_argument("--output", type=Path, default=Path("calibration"))
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def make_checkerboard(screen_width, screen_height, screen_width_mm, collected, target, message):
    columns = PATTERN_SIZE[0] + 1
    rows = PATTERN_SIZE[1] + 1
    square_px = min((screen_width - 80) // columns, (screen_height - 130) // rows)
    board_width = square_px * columns
    board_height = square_px * rows
    left = (screen_width - board_width) // 2
    top = 95 + max(0, (screen_height - 110 - board_height) // 2)

    canvas = np.full((screen_height, screen_width, 3), 118, dtype=np.uint8)
    for row in range(rows):
        for column in range(columns):
            color = 245 if (row + column) % 2 == 0 else 5
            x1 = left + column * square_px
            y1 = top + row * square_px
            cv2.rectangle(canvas, (x1, y1), (x1 + square_px, y1 + square_px), (color,) * 3, -1)

    title = f"CALIBRATION  {collected}/{target}"
    cv2.putText(canvas, title, (35, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.15, (255, 255, 255), 3, cv2.LINE_AA)
    cv2.putText(canvas, message, (37, 78), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (235, 235, 235), 1, cv2.LINE_AA)

    square_size_m = (square_px * screen_width_mm / screen_width) / 1000.0
    return canvas, square_px, square_size_m


def fetch_pair(receiver_url, timeout=1.0):
    with urllib.request.urlopen(f"{receiver_url}/pair.bin?t={time.time_ns()}", timeout=timeout) as response:
        packet = response.read()
    if len(packet) < PACKET_HEADER.size:
        raise ValueError("short pair packet")
    magic, version, sequence, timestamp_us, wide_size, ultra_size = PACKET_HEADER.unpack_from(packet)
    if magic != b"MCAM" or version != 1:
        raise ValueError("invalid pair packet")
    start = PACKET_HEADER.size
    wide_bytes = packet[start : start + wide_size]
    ultra_bytes = packet[start + wide_size : start + wide_size + ultra_size]
    if len(wide_bytes) != wide_size or len(ultra_bytes) != ultra_size:
        raise ValueError("incomplete pair packet")
    wide = cv2.imdecode(np.frombuffer(wide_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    ultra = cv2.imdecode(np.frombuffer(ultra_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if wide is None or ultra is None:
        raise ValueError("JPEG decode failed")
    return sequence, timestamp_us, wide, ultra, wide_bytes, ultra_bytes


def find_corners(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    flags = cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    found, corners = cv2.findChessboardCornersSB(gray, PATTERN_SIZE, flags=flags)
    return found, corners


def pose_descriptor(corners, image_shape):
    points = corners.reshape(-1, 2)
    height, width = image_shape[:2]
    center = points.mean(axis=0) / np.array([width, height], dtype=np.float32)
    area = cv2.contourArea(cv2.convexHull(points.astype(np.float32))) / float(width * height)
    row_vector = points[PATTERN_SIZE[0] - 1] - points[0]
    angle = math.atan2(float(row_vector[1]), float(row_vector[0]))
    return np.array([center[0], center[1], math.sqrt(max(area, 0.0)), math.cos(angle), math.sin(angle)])


def is_new_view(descriptor, accepted):
    if not accepted:
        return True
    for previous in accepted:
        center_change = np.linalg.norm(descriptor[:2] - previous[:2])
        scale_change = abs(descriptor[2] - previous[2])
        angle_change = math.acos(float(np.clip(np.dot(descriptor[3:], previous[3:]), -1.0, 1.0)))
        if center_change < 0.055 and scale_change < 0.025 and angle_change < math.radians(6):
            return False
    return True


def object_points(square_size_m):
    points = np.zeros((PATTERN_SIZE[0] * PATTERN_SIZE[1], 3), np.float32)
    points[:, :2] = np.mgrid[0 : PATTERN_SIZE[0], 0 : PATTERN_SIZE[1]].T.reshape(-1, 2)
    points *= square_size_m
    return points


def run_calibration(captures, square_size_m, output_dir):
    object_sets = [object_points(square_size_m) for _ in captures]
    wide_points = [entry[0].astype(np.float32) for entry in captures]
    ultra_points = [entry[1].astype(np.float32) for entry in captures]
    image_size = captures[0][2]

    wide_rms, wide_k, wide_d, _, _ = cv2.calibrateCamera(
        object_sets, wide_points, image_size, None, None
    )
    ultra_rms, ultra_k, ultra_d, _, _ = cv2.calibrateCamera(
        object_sets, ultra_points, image_size, None, None
    )

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 150, 1e-7)
    stereo_rms, wide_k, wide_d, ultra_k, ultra_d, rotation, translation, essential, fundamental = cv2.stereoCalibrate(
        object_sets,
        wide_points,
        ultra_points,
        wide_k,
        wide_d,
        ultra_k,
        ultra_d,
        image_size,
        criteria=criteria,
        flags=cv2.CALIB_FIX_INTRINSIC,
    )

    wide_rectification, ultra_rectification, wide_projection, ultra_projection, q_matrix, wide_roi, ultra_roi = cv2.stereoRectify(
        wide_k,
        wide_d,
        ultra_k,
        ultra_d,
        image_size,
        rotation,
        translation,
        flags=cv2.CALIB_ZERO_DISPARITY,
        alpha=0,
    )
    wide_map_x, wide_map_y = cv2.initUndistortRectifyMap(
        wide_k, wide_d, wide_rectification, wide_projection, image_size, cv2.CV_32FC1
    )
    ultra_map_x, ultra_map_y = cv2.initUndistortRectifyMap(
        ultra_k, ultra_d, ultra_rectification, ultra_projection, image_size, cv2.CV_32FC1
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output_dir / "stereo_calibration.npz",
        wide_camera_matrix=wide_k,
        wide_distortion=wide_d,
        ultra_camera_matrix=ultra_k,
        ultra_distortion=ultra_d,
        rotation=rotation,
        translation=translation,
        essential=essential,
        fundamental=fundamental,
        wide_rectification=wide_rectification,
        ultra_rectification=ultra_rectification,
        wide_projection=wide_projection,
        ultra_projection=ultra_projection,
        disparity_to_depth=q_matrix,
        wide_map_x=wide_map_x,
        wide_map_y=wide_map_y,
        ultra_map_x=ultra_map_x,
        ultra_map_y=ultra_map_y,
        image_size=np.array(image_size),
        square_size_m=np.array(square_size_m),
    )

    baseline_m = float(np.linalg.norm(translation))
    summary = {
        "samples": len(captures),
        "image_size": list(image_size),
        "pattern_inner_corners": list(PATTERN_SIZE),
        "square_size_m": square_size_m,
        "wide_rms_pixels": float(wide_rms),
        "ultra_rms_pixels": float(ultra_rms),
        "stereo_rms_pixels": float(stereo_rms),
        "estimated_baseline_m": baseline_m,
        "result_file": "stereo_calibration.npz",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    last_wide, last_ultra = captures[-1][3], captures[-1][4]
    rectified_wide = cv2.remap(last_wide, wide_map_x, wide_map_y, cv2.INTER_LINEAR)
    rectified_ultra = cv2.remap(last_ultra, ultra_map_x, ultra_map_y, cv2.INTER_LINEAR)
    preview = np.hstack([rectified_wide, rectified_ultra])
    for y in range(20, preview.shape[0], 40):
        cv2.line(preview, (0, y), (preview.shape[1] - 1, y), (0, 255, 0), 1)
    cv2.imwrite(str(output_dir / "rectified_preview.jpg"), preview)
    return summary


def beep():
    try:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Pop.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def self_test(screen_width_mm):
    board, square_px, square_size_m = make_checkerboard(1280, 828, screen_width_mm, 0, 25, "SELF TEST")
    board_only = board[95:, :]
    found, corners = find_corners(board_only)
    if not found or corners is None or len(corners) != PATTERN_SIZE[0] * PATTERN_SIZE[1]:
        raise RuntimeError("checkerboard self-test failed")
    print(f"self-test OK: square={square_px}px, {square_size_m * 1000:.2f}mm, corners={len(corners)}")


def main():
    args = parse_args()
    if args.self_test:
        self_test(args.screen_width_mm)
        return

    captures_dir = args.output / "captures"
    if captures_dir.exists():
        shutil.rmtree(captures_dir)
    captures_dir.mkdir(parents=True, exist_ok=True)

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    screen_width = 1280
    screen_height = 828
    captures = []
    descriptors = []
    last_sequence = None
    last_capture_time = 0.0
    message = "Aim both rear cameras at the board and move the iPhone slowly"
    square_size_m = 0.0

    print("Live calibration started. Press ESC to stop.", flush=True)
    try:
        while len(captures) < args.target:
            board, square_px, square_size_m = make_checkerboard(
                screen_width,
                screen_height,
                args.screen_width_mm,
                len(captures),
                args.target,
                message,
            )
            cv2.imshow(WINDOW_NAME, board)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                print("Calibration cancelled.", flush=True)
                return

            try:
                sequence, _, wide, ultra, wide_bytes, ultra_bytes = fetch_pair(args.receiver, timeout=0.6)
            except (urllib.error.URLError, TimeoutError, ValueError):
                message = "Waiting for iPhone wireless frames..."
                time.sleep(0.05)
                continue
            if sequence == last_sequence:
                time.sleep(0.02)
                continue
            last_sequence = sequence

            wide_found, wide_corners = find_corners(wide)
            ultra_found, ultra_corners = find_corners(ultra)
            if not (wide_found and ultra_found):
                message = "Move closer: the full board must be visible in BOTH cameras"
                continue

            descriptor = pose_descriptor(wide_corners, wide.shape)
            if time.monotonic() - last_capture_time < 0.65 or not is_new_view(descriptor, descriptors):
                message = "Detected. Move or tilt the iPhone for a different view"
                continue

            index = len(captures) + 1
            (captures_dir / f"wide_{index:02d}.jpg").write_bytes(wide_bytes)
            (captures_dir / f"ultra_{index:02d}.jpg").write_bytes(ultra_bytes)
            captures.append((wide_corners, ultra_corners, (wide.shape[1], wide.shape[0]), wide, ultra))
            descriptors.append(descriptor)
            last_capture_time = time.monotonic()
            message = f"Captured {index}/{args.target}. Keep moving slowly"
            print(f"captured {index}/{args.target}, sequence={sequence}", flush=True)
            beep()
    finally:
        cv2.destroyAllWindows()

    print("Computing stereo calibration...", flush=True)
    summary = run_calibration(captures, square_size_m, args.output)
    print(json.dumps(summary, indent=2), flush=True)
    print(f"Calibration saved to {args.output / 'stereo_calibration.npz'}", flush=True)


if __name__ == "__main__":
    main()
