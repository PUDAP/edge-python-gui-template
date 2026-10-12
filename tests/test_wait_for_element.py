"""Completion checks use observations, never repeat the triggering action."""

import unittest
from types import SimpleNamespace
from unittest import mock

import gui_driver
from test_driver import elem, make_driver


class WaitForElementTests(unittest.TestCase):
    def make_observations(self, observations):
        d = make_driver(_last_elements=[])
        frames = iter(observations)
        def capture(**kwargs):
            d._cua._last_elements = next(frames)
            return {"image_b64": "PNG", "capture": None, "elements": d._cua._last_elements}
        d.capture_screenshot = mock.Mock(side_effect=capture)
        return d

    def test_waits_for_enabled_exact_match(self):
        d = self.make_observations([
            [elem(1, "Ready soon")], [elem(2, "Ready", enabled=False)], [elem(3, "Ready")],
        ])
        with mock.patch.object(gui_driver.time, "sleep"):
            self.assertEqual(d.wait_for_element("Ready")["element_index"], 3)
        self.assertEqual(d.capture_screenshot.call_count, 3)

    def test_known_error_wins_over_success(self):
        d = self.make_observations([[elem(1, "Ready"), elem(2, "Import failed", enabled=False)]])
        with self.assertRaisesRegex(RuntimeError, "Application reported an error"):
            d.wait_for_element("Ready", error_labels=["Import failed"])

    def test_ambiguity_and_capture_failures_are_not_retried(self):
        d = self.make_observations([[elem(1, "Ready"), elem(2, "Ready")]])
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            d.wait_for_element("Ready")
        self.assertEqual(d.capture_screenshot.call_count, 1)
        d.capture_screenshot = mock.Mock(side_effect=RuntimeError("connection lost"))
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            d.wait_for_element("Ready")
        d.capture_screenshot.assert_called_once()

    def test_timeout_and_bounded_sleep(self):
        d = self.make_observations([[], []])
        clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[0, 0.8, 1]), sleep=mock.Mock())
        with mock.patch.object(gui_driver, "time", clock):
            with self.assertRaisesRegex(TimeoutError, "Ready"):
                d.wait_for_element("Ready", timeout=1)
        self.assertAlmostEqual(clock.sleep.call_args.args[0], 0.2)

    def test_zero_timeout_checks_once_and_invalid_arguments_do_not_capture(self):
        d = self.make_observations([[elem(1, "Ready")], []])
        self.assertEqual(d.wait_for_element("Ready", timeout=0)["label"], "Ready")
        with self.assertRaises(TimeoutError):
            d.wait_for_element("Ready", timeout=0)
        d.capture_screenshot.reset_mock()
        for options in ({"timeout": -1}, {"timeout": float("nan")},
                        {"poll_interval": 0}, {"poll_interval": float("inf")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                d.wait_for_element("Ready", **options)
        d.capture_screenshot.assert_not_called()
