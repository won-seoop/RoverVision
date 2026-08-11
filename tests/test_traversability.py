import unittest

import numpy as np

from live_depth import GRID_TOP_RATIO, StereoDepthEstimator


class TraversabilityTest(unittest.TestCase):
    def test_flat_ground_with_raised_center_obstacle(self):
        estimator = StereoDepthEstimator("calibration/stereo_calibration.npz")
        height, width = 360, 640
        ys, xs = np.indices((height, width))
        ray_y = (ys - estimator.center_y) / estimator.focal_y_pixels

        normal = np.array([0.0, -0.94, -0.342], dtype=np.float32)
        normal /= np.linalg.norm(normal)
        camera_height = 0.42
        denominator = normal[1] * ray_y + normal[2]
        depth = (-camera_height / denominator).astype(np.float32)
        valid = (
            (ys >= int(height * GRID_TOP_RATIO))
            & (depth >= 0.20)
            & (depth <= 3.00)
        )
        depth[~valid] = np.nan

        obstacle = (
            (xs >= 256)
            & (xs < 384)
            & (ys >= 283)
            & (ys < 360)
            & valid
        )
        depth[obstacle] = -(camera_height - 0.15) / denominator[obstacle]

        result = estimator.analyze_traversability(depth, valid)

        self.assertTrue(result["ground_plane_found"])
        self.assertEqual(result["cells"][12]["state"], "BLOCKED")
        self.assertGreaterEqual(result["counts"]["PASSABLE"], 12)


if __name__ == "__main__":
    unittest.main()
