import unittest

from folderbridge_mcp.dpi import (
    fitted_window_size,
    scale_for_dpi,
    scaled_pixels,
    tk_scaling_for_dpi,
)


class DpiTests(unittest.TestCase):
    def test_windows_scale_follows_common_dpi_values(self) -> None:
        self.assertEqual(scale_for_dpi(96), 1.0)
        self.assertEqual(scale_for_dpi(120), 1.25)
        self.assertEqual(scale_for_dpi(144), 1.5)
        self.assertEqual(scale_for_dpi(192), 2.0)

    def test_invalid_dpi_falls_back_to_100_percent(self) -> None:
        self.assertEqual(scale_for_dpi(None), 1.0)
        self.assertEqual(scale_for_dpi(True), 1.0)
        self.assertEqual(scale_for_dpi(0), 1.0)
        self.assertEqual(scale_for_dpi(10_000), 1.0)

    def test_tk_scaling_uses_pixels_per_point(self) -> None:
        self.assertEqual(tk_scaling_for_dpi(96), 96 / 72)
        self.assertEqual(tk_scaling_for_dpi(144), 2.0)

    def test_pixel_measurements_scale_and_remain_visible(self) -> None:
        self.assertEqual(scaled_pixels(18, 1.5), 27)
        self.assertEqual(scaled_pixels(1, 0.5), 1)
        self.assertEqual(scaled_pixels(0, 2.0), 0)

    def test_initial_window_scales_but_stays_on_screen(self) -> None:
        self.assertEqual(fitted_window_size(96, 1920, 1080), (940, 820))
        self.assertEqual(fitted_window_size(144, 2560, 1440), (1410, 1230))
        self.assertEqual(fitted_window_size(192, 1920, 1080), (1766, 972))

    def test_96_to_144_to_96_has_no_cumulative_metric_drift(self) -> None:
        logical_metrics = (2, 6, 9, 18, 115, 285, 320, 820)
        first_96 = tuple(scaled_pixels(value, scale_for_dpi(96)) for value in logical_metrics)
        at_144 = tuple(scaled_pixels(value, scale_for_dpi(144)) for value in logical_metrics)
        second_96 = tuple(scaled_pixels(value, scale_for_dpi(96)) for value in logical_metrics)
        self.assertEqual(first_96, second_96)
        self.assertNotEqual(first_96, at_144)


if __name__ == "__main__":
    unittest.main()
