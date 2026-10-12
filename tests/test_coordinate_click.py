"""Offline checks for capture-bound coordinate input."""

import unittest
from types import SimpleNamespace
from unittest import mock

from test_driver import make_driver
from cua_client import CuaDriverClient


class CoordinateClickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = CuaDriverClient()
        self.client._tools = dict.fromkeys([
            "get_window_state", "click", "list_windows", "bring_to_front", "press_key",
        ])
        self.bounds = {"x": 10, "y": 20, "width": 200, "height": 100}
        self.window = {"pid": 1, "window_id": 2, "title": "Demo",
                       "bounds": self.bounds.copy(), "is_on_screen": True}
        self.result = SimpleNamespace(
            content=[SimpleNamespace(type="image", data="PNG")],
            structuredContent={"window_bounds": self.bounds.copy(),
                               "screenshot_width": 200, "screenshot_height": 100},
        )
        async def call(tool, args):
            if tool == "get_window_state":
                return self.result
            if tool == "list_windows":
                return SimpleNamespace(structuredContent={"windows": [self.window]})
            return SimpleNamespace(structuredContent={"ok": True})
        self.client._call = mock.AsyncMock(side_effect=call)
        self.client._matching_windows = mock.AsyncMock(return_value=[self.window])
        self.client._make_window_visible = mock.AsyncMock(return_value=self.window)
        await self.client.capture("Demo")
        self.capture_id = self.client._coordinate_capture["capture_id"]
        self.client._call.reset_mock()

    async def test_click_uses_captured_target_without_capture_or_resize(self):
        self.client._pid, self.client._window_id = 99, 100
        await self.client.click_at(5, 6, self.capture_id)
        self.assertEqual(self.client._call.call_args_list, [
            mock.call("list_windows", {"on_screen_only": False}),
            mock.call("bring_to_front", {"pid": 1, "window_id": 2}),
            mock.call("list_windows", {"on_screen_only": False}),
            mock.call("click", {"pid": 1, "window_id": 2, "x": 5, "y": 6,
                                "button": "left", "delivery_mode": "foreground"}),
        ])
        with self.assertRaisesRegex(RuntimeError, "stale"):
            await self.client.click_at(5, 6, self.capture_id)

    async def test_new_capture_and_input_invalidate_id(self):
        await self.client.capture("Demo")
        with self.assertRaisesRegex(RuntimeError, "stale"):
            await self.client.click_at(5, 6, self.capture_id)
        new_id = self.client._coordinate_capture["capture_id"]
        await self.client.key("escape")
        with self.assertRaisesRegex(RuntimeError, "stale"):
            await self.client.click_at(5, 6, new_id)

    async def test_geometry_visibility_and_identity_changes_reject_input(self):
        for change in ({"bounds": {**self.bounds, "x": 30}},
                       {"bounds": {**self.bounds, "width": 300}},
                       {"minimized": True}, {"is_on_screen": False}, {"pid": 9}):
            with self.subTest(change=change):
                original = self.window.copy()
                await self.client.capture("Demo")
                capture_id = self.client._coordinate_capture["capture_id"]
                self.window.update(change)
                self.client._call.reset_mock()
                with self.assertRaises(RuntimeError):
                    await self.client.click_at(5, 6, capture_id)
                self.assertEqual(self.client._call.call_count, 1)
                self.window.clear()
                self.window.update(original)

    async def test_geometry_change_during_focus_rejects_click(self):
        original = self.client._call.side_effect
        async def call(tool, args):
            if tool == "bring_to_front":
                self.window["bounds"]["x"] = 99
            return await original(tool, args)
        self.client._call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, "moved"):
            await self.client.click_at(5, 6, self.capture_id)
        self.assertNotIn("click", [call.args[0] for call in self.client._call.call_args_list])

    async def test_invalid_coordinates_and_id_send_no_calls(self):
        for x, y in ((-1, 0), (200, 0), (0, 100), (1.5, 0), (True, 0)):
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                await self.client.click_at(x, y, self.capture_id)
        with self.assertRaises(RuntimeError):
            await self.client.click_at(1, 1, "unknown")
        self.client._call.assert_not_awaited()

    async def test_failed_input_consumes_id_without_retry(self):
        original = self.client._call.side_effect
        async def call(tool, args):
            if tool == "click":
                return SimpleNamespace(isError=True)
            return await original(tool, args)
        self.client._call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, "input rejected"):
            await self.client.click_at(5, 6, self.capture_id)
        self.assertIsNone(self.client._coordinate_capture)

    async def test_capture_without_geometry_cannot_authorize_clicks(self):
        self.result.structuredContent = {}
        await self.client.capture("Demo")
        self.assertIsNone(self.client._coordinate_capture)

    async def test_native_capture_requests_elements_only_when_enabled(self):
        elements = [{"element_index": 1, "label": "Start"}]
        self.result.structuredContent["elements"] = elements
        for include_elements in (True, False):
            with self.subTest(include_elements=include_elements):
                self.client._call.reset_mock()
                await self.client.capture("Demo", include_elements=include_elements)
                self.client._call.assert_awaited_once_with("get_window_state", {
                    "pid": 1, "window_id": 2, "include_screenshot": True,
                    "include_accessibility_tree": include_elements,
                })
                self.assertEqual(self.client._last_elements, elements if include_elements else [])

    async def test_wrapper_translates_boolean_to_backend_mode(self):
        self.client._tools = {"computer_use": None}
        self.result.structuredContent["elements"] = [{"element_index": 1}]
        self.client._call = mock.AsyncMock(return_value=self.result)
        for include_elements, mode in ((True, "som"), (False, "screenshot")):
            with self.subTest(include_elements=include_elements):
                await self.client.capture("Demo", include_elements=include_elements)
                self.client._call.assert_awaited_with("computer_use", {
                    "action": "capture", "mode": mode, "app": "Demo",
                })
                self.assertEqual(bool(self.client._last_elements), include_elements)

    async def test_screenshot_only_backend_rejects_element_request(self):
        self.client._tools = {"screenshot": None}
        with self.assertRaisesRegex(RuntimeError, "does not support accessibility"):
            await self.client.capture("Demo", include_elements=True)
        self.client._call.assert_not_awaited()
        self.assertEqual(self.client._last_elements, [])

    async def test_capture_rejects_non_boolean_flag(self):
        with self.assertRaisesRegex(TypeError, "must be a bool"):
            await self.client.capture("Demo", include_elements="som")
        self.client._call.assert_not_awaited()


class CoordinateCommandTests(unittest.TestCase):
    def test_command_returns_capture_id_and_click_does_not_capture(self):
        d = make_driver(
            capture=mock.AsyncMock(return_value=SimpleNamespace(
                content=[SimpleNamespace(type="image", data="PNG")],
            )),
            _coordinate_capture={"capture_id": "capture", "width": 200, "height": 100,
                                 "pid": 1, "window_id": 2,
                                 "bounds": {"x": 10, "y": 20, "width": 200, "height": 100}},
            click_at=mock.AsyncMock(return_value=SimpleNamespace(structuredContent={"effect": "ok"})),
        )
        screenshot = d.capture_screenshot()
        self.assertEqual(set(screenshot), {"image_b64", "capture"})
        self.assertEqual(screenshot["capture"], d._cua._coordinate_capture)
        screenshot["capture"]["capture_id"] = "modified"
        screenshot["capture"]["bounds"]["x"] = 999
        self.assertEqual(d._cua._coordinate_capture["capture_id"], "capture")
        self.assertEqual(d._cua._coordinate_capture["bounds"]["x"], 10)
        d._cua.capture.reset_mock()
        result = d.click_at(5, 6, "capture")
        d._cua.capture.assert_not_awaited()
        d._cua.click_at.assert_awaited_once_with(x=5, y=6, capture_id="capture")
        self.assertNotIn("image_b64", result)

    def test_screenshot_without_coordinate_context_returns_null_capture(self):
        d = make_driver(capture=mock.AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="image", data="PNG")],
        )))
        self.assertEqual(d.capture_screenshot(), {"image_b64": "PNG", "capture": None})
