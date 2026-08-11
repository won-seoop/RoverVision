#!/usr/bin/env python3
import cv2

from live_calibrate import DEFAULT_SCREEN_WIDTH_MM, WINDOW_NAME, make_checkerboard


def main():
    board, square_px, square_size_m = make_checkerboard(
        1280,
        828,
        DEFAULT_SCREEN_WIDTH_MM,
        0,
        0,
        "Checkerboard only - press ESC to close",
    )
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    print(f"checkerboard: square={square_px}px, {square_size_m * 1000:.2f}mm")
    while True:
        cv2.imshow(WINDOW_NAME, board)
        if cv2.waitKey(50) & 0xFF == 27:
            break
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
