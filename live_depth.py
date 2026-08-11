#!/usr/bin/env python3
import argparse
import time
import urllib.error
from pathlib import Path

import cv2
import numpy as np

from live_calibrate import fetch_pair

WINDOW_NAME = "RoverVision Live Stereo Depth"
MIN_DEPTH_M = 0.20
MAX_DEPTH_M = 3.00
OBSTACLE_THRESHOLD_M = 0.60
GRID_ROWS = 3
GRID_COLS = 5
GRID_TOP_RATIO = 0.36
GROUND_INLIER_M = 0.045
OBSTACLE_HEIGHT_M = 0.10


def parse_args():
    parser = argparse.ArgumentParser(description="Live stereo depth from iPhone Wide + Ultra Wide")
    parser.add_argument("--receiver", default="http://127.0.0.1:8080")
    parser.add_argument("--calibration", type=Path, default=Path("calibration/stereo_calibration.npz"))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--wide", type=Path, default=Path("calibration/captures/wide_25.jpg"))
    parser.add_argument("--ultra", type=Path, default=Path("calibration/captures/ultra_25.jpg"))
    return parser.parse_args()


class StereoDepthEstimator:
    def __init__(self, calibration_path):
        calibration = np.load(calibration_path)
        self.wide_map_x = calibration["wide_map_x"]
        self.wide_map_y = calibration["wide_map_y"]
        self.ultra_map_x = calibration["ultra_map_x"]
        self.ultra_map_y = calibration["ultra_map_y"]
        wide_projection = calibration["wide_projection"]
        ultra_projection = calibration["ultra_projection"]
        self.focal_pixels = float(wide_projection[0, 0])
        self.focal_y_pixels = float(wide_projection[1, 1])
        self.center_x = float(wide_projection[0, 2])
        self.center_y = float(wide_projection[1, 2])
        self.baseline_m = abs(float(ultra_projection[0, 3]) / self.focal_pixels)
        self.min_disparity = -96
        self.num_disparities = 128
        block_size = 5
        self.matcher = cv2.StereoSGBM.create(
            minDisparity=self.min_disparity,
            numDisparities=self.num_disparities,
            blockSize=block_size,
            P1=8 * block_size * block_size,
            P2=32 * block_size * block_size,
            disp12MaxDiff=2,
            uniquenessRatio=8,
            speckleWindowSize=80,
            speckleRange=2,
            preFilterCap=31,
            mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
        )
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self.rng = np.random.default_rng(20260811)

    def estimate(self, wide, ultra):
        rectified_wide = cv2.remap(wide, self.wide_map_x, self.wide_map_y, cv2.INTER_LINEAR)
        rectified_ultra = cv2.remap(ultra, self.ultra_map_x, self.ultra_map_y, cv2.INTER_LINEAR)
        wide_gray = self.clahe.apply(cv2.cvtColor(rectified_wide, cv2.COLOR_BGR2GRAY))
        ultra_gray = self.clahe.apply(cv2.cvtColor(rectified_ultra, cv2.COLOR_BGR2GRAY))

        disparity = self.matcher.compute(wide_gray, ultra_gray).astype(np.float32) / 16.0
        valid = (disparity < -0.5) & (disparity > self.min_disparity + 1)
        depth = np.full(disparity.shape, np.nan, dtype=np.float32)
        depth[valid] = (self.focal_pixels * self.baseline_m) / (-disparity[valid])
        valid &= (depth >= MIN_DEPTH_M) & (depth <= MAX_DEPTH_M)
        depth[~valid] = np.nan
        return rectified_wide, rectified_ultra, disparity, depth, valid

    def points_from_pixels(self, xs, ys, depths):
        x = (xs.astype(np.float32) - self.center_x) * depths / self.focal_pixels
        y = (ys.astype(np.float32) - self.center_y) * depths / self.focal_y_pixels
        return np.column_stack((x, y, depths))

    def fit_ground_plane(self, depth, valid):
        height, _ = depth.shape
        candidate_mask = valid.copy()
        candidate_mask[: int(height * GRID_TOP_RATIO)] = False
        ys, xs = np.nonzero(candidate_mask)
        if xs.size < 120:
            return None, 0.0

        depths = depth[ys, xs]
        points = self.points_from_pixels(xs, ys, depths)
        if points.shape[0] > 1600:
            indices = self.rng.choice(points.shape[0], 1600, replace=False)
            points = points[indices]

        best_mask = None
        best_score = 0
        for _ in range(70):
            sample = points[self.rng.choice(points.shape[0], 3, replace=False)]
            normal = np.cross(sample[1] - sample[0], sample[2] - sample[0])
            length = np.linalg.norm(normal)
            if length < 1e-6:
                continue
            normal /= length
            if normal[1] > 0:
                normal = -normal
            # A floor normal points mainly upward in camera coordinates. This
            # rejects walls and objects that happen to form a large plane.
            if -normal[1] < 0.48:
                continue
            offset = -float(normal @ sample[0])
            distances = np.abs(points @ normal + offset)
            inliers = distances < GROUND_INLIER_M
            score = int(inliers.sum())
            if score > best_score:
                best_score = score
                best_mask = inliers

        if best_mask is None or best_score < max(70, int(points.shape[0] * 0.10)):
            return None, 0.0

        floor_points = points[best_mask]
        center = floor_points.mean(axis=0)
        _, _, vectors = np.linalg.svd(floor_points - center, full_matrices=False)
        normal = vectors[-1]
        if normal[1] > 0:
            normal = -normal
        if -normal[1] < 0.48:
            return None, 0.0
        offset = -float(normal @ center)
        camera_height = offset
        if not 0.15 <= camera_height <= 1.20:
            return None, 0.0
        confidence = best_score / points.shape[0]
        return (normal.astype(np.float32), offset), float(confidence)

    def analyze_traversability(self, depth, valid):
        height, width = depth.shape
        plane, confidence = self.fit_ground_plane(depth, valid)
        top = int(height * GRID_TOP_RATIO)
        bottom = height
        cells = []

        for row in range(GRID_ROWS):
            y1 = top + (bottom - top) * row // GRID_ROWS
            y2 = top + (bottom - top) * (row + 1) // GRID_ROWS
            for col in range(GRID_COLS):
                x1 = width * col // GRID_COLS
                x2 = width * (col + 1) // GRID_COLS
                cell_mask = valid[y1:y2, x1:x2]
                ys_local, xs_local = np.nonzero(cell_mask)
                valid_pixels = int(xs_local.size)
                state = "UNKNOWN"
                distance = None
                obstacle_height = None

                if valid_pixels:
                    cell_depths = depth[y1:y2, x1:x2][cell_mask]
                    distance = float(np.median(cell_depths))

                if plane is not None and valid_pixels >= 30:
                    xs = xs_local + x1
                    ys = ys_local + y1
                    points = self.points_from_pixels(xs, ys, cell_depths)
                    normal, offset = plane
                    heights = points @ normal + offset
                    obstacle_pixels = int((heights >= OBSTACLE_HEIGHT_M).sum())
                    ground_pixels = int((np.abs(heights) <= GROUND_INLIER_M * 1.5).sum())
                    obstacle_height = float(np.percentile(heights, 90))
                    if obstacle_pixels >= max(10, int(valid_pixels * 0.08)):
                        state = "BLOCKED"
                    elif ground_pixels >= max(12, int(valid_pixels * 0.10)):
                        state = "PASSABLE"

                cells.append(
                    {
                        "row": row,
                        "col": col,
                        "box": (x1, y1, x2, y2),
                        "state": state,
                        "distance_m": distance,
                        "obstacle_height_m": obstacle_height,
                        "valid_pixels": valid_pixels,
                    }
                )

        counts = {
            state: sum(cell["state"] == state for cell in cells)
            for state in ("PASSABLE", "BLOCKED", "UNKNOWN")
        }
        return {
            "ground_plane_found": plane is not None,
            "ground_confidence": confidence,
            "cells": cells,
            "counts": counts,
        }


def robust_distance(depth, valid, roi):
    x1, y1, x2, y2 = roi
    values = depth[y1:y2, x1:x2]
    mask = valid[y1:y2, x1:x2]
    values = values[mask]
    if values.size < 80:
        return None, int(values.size)
    lower = np.percentile(values, 20)
    upper = np.percentile(values, 80)
    trimmed = values[(values >= lower) & (values <= upper)]
    return float(np.median(trimmed)), int(values.size)


def obstacle_state(distance):
    if distance is None:
        return "UNKNOWN"
    if distance <= OBSTACLE_THRESHOLD_M:
        return "OBSTACLE"
    return "CLEAR"


def depth_colormap(depth, valid):
    normalized = np.zeros(depth.shape, dtype=np.uint8)
    clipped = np.clip(depth, MIN_DEPTH_M, MAX_DEPTH_M)
    normalized[valid] = np.uint8(
        255.0 * (MAX_DEPTH_M - clipped[valid]) / (MAX_DEPTH_M - MIN_DEPTH_M)
    )
    colored = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
    colored[~valid] = 0
    return colored


def draw_traversability(image, traversability):
    if traversability is None:
        return
    colors = {
        "PASSABLE": (0, 210, 0),
        "BLOCKED": (0, 0, 255),
        "UNKNOWN": (0, 165, 255),
    }
    labels = {"PASSABLE": "PASS", "BLOCKED": "BLOCK", "UNKNOWN": "?"}
    overlay = image.copy()
    for cell in traversability["cells"]:
        x1, y1, x2, y2 = cell["box"]
        cv2.rectangle(overlay, (x1 + 1, y1 + 1), (x2 - 1, y2 - 1), colors[cell["state"]], -1)
    cv2.addWeighted(overlay, 0.28, image, 0.72, 0, image)

    for cell in traversability["cells"]:
        x1, y1, x2, y2 = cell["box"]
        color = colors[cell["state"]]
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
        distance = cell["distance_m"]
        distance_text = "--" if distance is None else f"{distance:.1f}m"
        cv2.putText(
            image,
            f"{labels[cell['state']]} {distance_text}",
            (x1 + 5, y1 + 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )


def make_dashboard(rectified_wide, rectified_ultra, depth, valid, fps, traversability=None):
    height, width = depth.shape
    roi_width = max(80, width // 5)
    roi_height = max(70, height // 4)
    center_x, center_y = width // 2, height // 2
    roi = (
        center_x - roi_width // 2,
        center_y - roi_height // 2,
        center_x + roi_width // 2,
        center_y + roi_height // 2,
    )
    distance, valid_pixels = robust_distance(depth, valid, roi)
    state = obstacle_state(distance)
    depth_view = depth_colormap(depth, valid)

    x1, y1, x2, y2 = roi
    state_colors = {
        "OBSTACLE": (0, 0, 255),
        "CLEAR": (0, 255, 0),
        "UNKNOWN": (0, 165, 255),
    }
    box_color = state_colors[state]
    cv2.rectangle(rectified_wide, (x1, y1), (x2, y2), box_color, 2)
    cv2.rectangle(depth_view, (x1, y1), (x2, y2), box_color, 2)
    distance_text = f"CENTER: {distance:.2f} m" if distance is not None else "CENTER: NO DEPTH"
    cv2.putText(rectified_wide, distance_text, (18, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.82, box_color, 2, cv2.LINE_AA)
    cv2.putText(
        rectified_wide,
        f"{state}  LIMIT: {OBSTACLE_THRESHOLD_M:.2f} m",
        (18, 66),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.78,
        box_color,
        2,
        cv2.LINE_AA,
    )
    cv2.putText(depth_view, f"DEPTH {MIN_DEPTH_M:.1f}-{MAX_DEPTH_M:.1f}m", (18, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(depth_view, "RED=CLOSE  BLUE=FAR", (18, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 1, cv2.LINE_AA)
    draw_traversability(rectified_wide, traversability)
    if traversability is not None:
        counts = traversability["counts"]
        ground = "GROUND OK" if traversability["ground_plane_found"] else "NO GROUND"
        cv2.putText(
            depth_view,
            f"TRAVERSABILITY: {ground}",
            (18, 92),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            depth_view,
            f"PASS {counts['PASSABLE']}  BLOCK {counts['BLOCKED']}  UNKNOWN {counts['UNKNOWN']}",
            (18, 118),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    cv2.putText(depth_view, f"{fps:.1f} fps | valid center pixels: {valid_pixels}", (18, height - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

    dashboard = np.hstack([rectified_wide, depth_view])
    return dashboard, distance, float(valid.mean())


def save_snapshot(output_dir, sequence, rectified_wide, rectified_ultra, disparity, depth, dashboard):
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_dir / f"depth_{sequence}"
    cv2.imwrite(str(prefix) + "_wide.jpg", rectified_wide)
    cv2.imwrite(str(prefix) + "_ultra.jpg", rectified_ultra)
    cv2.imwrite(str(prefix) + "_dashboard.jpg", dashboard)
    np.savez_compressed(str(prefix) + "_data.npz", disparity=disparity, depth_m=depth)
    print(f"snapshot saved: {prefix}_dashboard.jpg", flush=True)


def self_test(args):
    estimator = StereoDepthEstimator(args.calibration)
    wide = cv2.imread(str(args.wide))
    ultra = cv2.imread(str(args.ultra))
    if wide is None or ultra is None:
        raise FileNotFoundError("self-test images not found")
    rectified_wide, rectified_ultra, disparity, depth, valid = estimator.estimate(wide, ultra)
    traversability = estimator.analyze_traversability(depth, valid)
    dashboard, distance, valid_ratio = make_dashboard(
        rectified_wide, rectified_ultra, depth, valid, 0.0, traversability
    )
    output = Path("calibration/depth_self_test.jpg")
    cv2.imwrite(str(output), dashboard)
    finite = depth[np.isfinite(depth)]
    median_depth = float(np.median(finite)) if finite.size else None
    print(
        f"self-test: baseline={estimator.baseline_m * 1000:.2f}mm "
        f"valid={valid_ratio * 100:.1f}% median={median_depth} center={distance} output={output}"
    )
    if valid_ratio < 0.01:
        raise RuntimeError("stereo matcher produced too few valid depth pixels")


def main():
    args = parse_args()
    if args.self_test:
        self_test(args)
        return

    estimator = StereoDepthEstimator(args.calibration)
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    last_sequence = None
    fps = 0.0
    frame_count = 0
    fps_started = time.monotonic()
    last_result = None
    print(
        f"Live depth started: focal={estimator.focal_pixels:.2f}px "
        f"baseline={estimator.baseline_m * 1000:.2f}mm. Q=quit, S=save",
        flush=True,
    )

    try:
        while True:
            try:
                sequence, _, wide, ultra, _, _ = fetch_pair(args.receiver, timeout=0.8)
            except (urllib.error.URLError, TimeoutError, ValueError):
                waiting = np.zeros((360, 1280, 3), dtype=np.uint8)
                cv2.putText(waiting, "Waiting for iPhone frames...", (360, 190), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 200, 255), 2, cv2.LINE_AA)
                cv2.imshow(WINDOW_NAME, waiting)
                if cv2.waitKey(30) & 0xFF == ord("q"):
                    break
                continue
            if sequence == last_sequence:
                if cv2.waitKey(5) & 0xFF == ord("q"):
                    break
                continue
            last_sequence = sequence

            rectified_wide, rectified_ultra, disparity, depth, valid = estimator.estimate(wide, ultra)
            traversability = estimator.analyze_traversability(depth, valid)
            frame_count += 1
            elapsed = time.monotonic() - fps_started
            if elapsed >= 1.0:
                fps = frame_count / elapsed
                frame_count = 0
                fps_started = time.monotonic()
            dashboard, distance, valid_ratio = make_dashboard(
                rectified_wide.copy(), rectified_ultra, depth, valid, fps, traversability
            )
            last_result = (sequence, rectified_wide, rectified_ultra, disparity, depth, dashboard)
            cv2.imshow(WINDOW_NAME, dashboard)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q") or key == 27:
                break
            if key == ord("s") and last_result is not None:
                save_snapshot(Path("depth_snapshots"), *last_result)
            if sequence % 150 == 0:
                distance_text = "none" if distance is None else f"{distance:.2f}m"
                print(f"sequence={sequence} center={distance_text} valid={valid_ratio * 100:.1f}%", flush=True)
    finally:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
