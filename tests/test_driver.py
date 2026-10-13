"""Offline tests for the generic GUI driver logic (no display or cua-driver needed)."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from puda.command import resolve_command_names

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui_driver as driver  # noqa: E402
import cua_client  # noqa: E402
from cua_client import CuaDriverClient  # noqa: E402
from driver import Driver  # noqa: E402
from gui_driver import ElementNotFoundError, GuiDriver  # noqa: E402


def make_driver(**cua_attrs) -> GuiDriver:
    """A GuiDriver without a real event loop / cua-driver; coroutines run inline."""
    d = object.__new__(Driver)
    d.target_app = "Demo"
    d._last_status = {}
    d._run = lambda coro: asyncio.run(coro)
    d._cua = SimpleNamespace(**{"_coordinate_capture": None, **cua_attrs})
    return d


def elem(index, label, role="push button", **extra):
    return {"element_index": index, "label": label, "role": role, **extra}


class ElementClickTests(unittest.TestCase):
    def _driver(self, elements):
        clicked = []

        async def capture(app=None, include_elements=False):
            return SimpleNamespace(content=[SimpleNamespace(type="image", data="AAAA")])

        async def click(element=None, **kw):
            clicked.append(element)

        d = make_driver(capture=capture, click=click, _last_elements=elements, _capture_meta={})
        return d, clicked

    def test_clicks_exact_matching_element(self):
        d, clicked = self._driver([elem(1, "Cancel run"), elem(2, "Start run"), elem(3, "Start")])
        self.assertEqual(d.click_element("Start run")["clicked_element"], 2)
        self.assertEqual(clicked, [2])

    def test_raises_when_nothing_matches(self):
        d, clicked = self._driver([elem(1, "Cancel")])
        with mock.patch.object(driver.time, "sleep"):
            with self.assertRaises(RuntimeError):
                d.click_element("Save")
        self.assertEqual(clicked, [])

    def test_ignores_disabled_elements(self):
        d, clicked = self._driver([elem(1, "Start run", enabled=False)])
        with mock.patch.object(driver.time, "sleep"):
            with self.assertRaises(RuntimeError):
                d.click_element("Start run")

    def test_rejects_ambiguous_matches(self):
        d, clicked = self._driver([elem(9, "Start"), elem(2, "Start")])
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            d.click_element("Start")
        self.assertEqual(clicked, [])

    def test_rejects_element_outside_captured_window(self):
        d, clicked = self._driver([
            elem(1, "Start", frame={"x": 200, "y": 0, "w": 10, "h": 10}),
        ])
        d._cua._capture_meta = {"window_bounds": {"x": 0, "y": 0, "width": 100, "height": 100}}
        with self.assertRaisesRegex(RuntimeError, "outside the captured window"):
            d.click_element("Start")
        self.assertEqual(clicked, [])

    def test_lookup_is_read_only_and_role_disambiguates(self):
        d, clicked = self._driver([elem(1, "Start", role="text"), elem(2, "Start")])
        self.assertEqual(len(d.capture_screenshot(include_elements=True)["elements"]), 2)
        self.assertEqual(d.find_element("Start", "push button")["element_index"], 2)
        self.assertEqual(clicked, [])
        self.assertEqual(d.click_element("Start", "push button")["clicked_element"], 2)
        self.assertEqual(clicked, [2])

    def test_lookup_rejects_partial_case_mismatched_and_empty_labels(self):
        d, clicked = self._driver([elem(1, "Start run")])
        for label in ("Start", "start run", "Start run button"):
            with self.subTest(label=label), self.assertRaises(RuntimeError):
                d.click_element(label)
        with self.assertRaises(ValueError):
            d.click_element("")
        self.assertEqual(clicked, [])

    def test_action_resolves_new_index_after_lookup(self):
        d, clicked = self._driver([elem(1, "Start")])
        self.assertEqual(d.find_element("Start")["element_index"], 1)
        d._cua._last_elements = [elem(8, "Start")]
        d.click_element("Start")
        self.assertEqual(clicked, [8])

    def test_set_value_rejects_ambiguity_before_input(self):
        d, _ = self._driver([elem(1, "Temperature"), elem(2, "Temperature")])
        d._cua.set_value = mock.AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            d.set_value("Temperature", "37")
        d._cua.set_value.assert_not_awaited()
        d._cua._last_elements = [elem(2, "Temperature", role="edit")]
        d.set_value("Temperature", "37", role="edit")
        d._cua.set_value.assert_awaited_once_with(2, "37")

    def test_scroll_validates_before_capture_and_dispatch(self):
        d = make_driver(capture=mock.AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="image", data="PNG")],
        )), scroll=mock.AsyncMock())
        for direction, amount in (("diagonal", 3), ("down", 0), ("up", -1), ("left", 1.5)):
            with self.subTest(direction=direction, amount=amount), self.assertRaises(ValueError):
                d.scroll(direction, amount)
        d._cua.capture.assert_not_awaited()
        d._cua.scroll.assert_not_awaited()
        self.assertEqual(d.scroll("down", 2), {"direction": "down", "amount": 2})
        d._cua.capture.assert_awaited_once()
        d._cua.scroll.assert_awaited_once_with(direction="down", amount=2)


class InputDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_scroll_rejects_noop_result(self):
        client = CuaDriverClient()
        client._tools = {"scroll": None}
        client._call = mock.AsyncMock(return_value=SimpleNamespace(
            structuredContent={"effect": "suspected_noop"},
        ))
        with self.assertRaisesRegex(RuntimeError, "input rejected"):
            await client.scroll()

    async def test_failed_capture_discards_old_accessibility_snapshot(self):
        client = CuaDriverClient()
        client._tools = {"get_window_state": None}
        client._last_elements = [elem(1, "Old")]
        client._snapshot_id = "old"
        client._capture_meta = {"snapshot_id": "old"}
        client._matching_windows = mock.AsyncMock(side_effect=RuntimeError("gone"))
        with self.assertRaisesRegex(RuntimeError, "gone"):
            await client.capture("Demo", include_elements=True)
        self.assertEqual(client._last_elements, [])
        self.assertEqual(client._capture_meta, {})
        self.assertIsNone(client._snapshot_id)

    async def test_native_actions_target_captured_window(self):
        client = CuaDriverClient()
        client._tools = dict.fromkeys(["click", "type_text", "press_key"])
        client._pid, client._window_id, client._snapshot_id = 10, 20, "snapshot"
        client._call = mock.AsyncMock(return_value=SimpleNamespace(structuredContent={"ok": True}))

        await client.click(element=3)
        await client.type_text("hello", delivery_mode=None)
        await client.key("CTRL+S")

        self.assertEqual(client._call.call_args_list, [
            mock.call("click", {"button": "left", "pid": 10, "window_id": 20,
                                "delivery_mode": "foreground", "element_index": 3,
                                "snapshot_id": "snapshot"}),
            mock.call("type_text", {"text": "hello", "pid": 10, "window_id": 20}),
            mock.call("press_key", {"key": "s", "modifiers": ["ctrl"], "pid": 10,
                                    "window_id": 20, "delivery_mode": "foreground"}),
        ])

    async def test_native_input_without_capture_omits_target(self):
        client = CuaDriverClient()
        client._tools = dict.fromkeys(["click", "type_text", "press_key"])
        client._call = mock.AsyncMock(return_value=SimpleNamespace())
        await client.click(x=1, y=2)
        await client.type_text("hello")
        await client.key("escape")
        self.assertEqual(client._call.call_args_list, [
            mock.call("click", {"button": "left", "x": 1, "y": 2}),
            mock.call("type_text", {"text": "hello"}),
            mock.call("press_key", {"key": "escape", "modifiers": []}),
        ])

    async def test_wrapper_keeps_its_own_argument_schema(self):
        client = CuaDriverClient()
        client._tools = {"computer_use": None}
        client._pid, client._window_id = 10, 20
        client._call = mock.AsyncMock(return_value=SimpleNamespace())
        await client.click(element=3)
        await client.type_text("hello")
        await client.key("ctrl+s", capture_after=True)
        self.assertEqual(client._call.call_args_list, [
            mock.call("computer_use", {"action": "click", "button": "left", "element": 3}),
            mock.call("computer_use", {"action": "type", "text": "hello"}),
            mock.call("computer_use", {"action": "key", "keys": "ctrl+s", "capture_after": True}),
        ])


class FileDialogDetectionTests(unittest.TestCase):
    def _client(self, windows):
        client = object.__new__(CuaDriverClient)

        async def call(tool, args):
            return SimpleNamespace(structuredContent={"windows": windows})

        client._call = call
        return client

    def test_finds_gnome_portal_dialog(self):
        win = {"title": "Open Files", "app_name": "xdg-desktop-portal-gnome", "pid": 1, "window_id": 2}
        found = asyncio.run(self._client([win])._find_file_dialog("Demo"))
        self.assertEqual(found, win)

    def test_finds_windows_open_dialog_owned_by_app(self):
        win = {"title": "Open", "app_name": "Demo.exe", "pid": 1, "window_id": 2}
        self.assertEqual(asyncio.run(self._client([win])._find_file_dialog("Demo")), win)

    def test_ignores_dialog_of_unrelated_app(self):
        win = {"title": "Open", "app_name": "SomethingElse", "pid": 1, "window_id": 2}
        self.assertIsNone(asyncio.run(self._client([win])._find_file_dialog("Demo")))

    def test_ignores_non_dialog_windows(self):
        win = {"title": "Demo", "app_name": "Demo", "pid": 1, "window_id": 2}
        self.assertIsNone(asyncio.run(self._client([win])._find_file_dialog("Demo")))

    def test_prefers_target_app_dialog_over_portal(self):
        portal = {"title": "Open Files", "app_name": "xdg-desktop-portal-gnome"}
        target = {"title": "Open", "app_name": "Demo.exe"}
        for windows in ([portal, target], [target, portal]):
            with self.subTest(windows=windows):
                self.assertEqual(asyncio.run(self._client(windows)._find_file_dialog("Demo")), target)


class WaitFileDialogTests(unittest.TestCase):
    def test_waits_for_appearance_and_disappearance(self):
        dialog = {"title": "Open", "app_name": "Demo"}
        for appear, observations, expected in (
            (True, [None, dialog], dialog),
            (False, [dialog, None], None),
        ):
            with self.subTest(appear=appear):
                finder = mock.AsyncMock(side_effect=observations)
                d = make_driver(_find_file_dialog=finder)
                with mock.patch.object(driver.time, "sleep"):
                    self.assertEqual(d._wait_file_dialog(appear=appear), expected)
                self.assertEqual(finder.await_count, 2)

    def test_zero_timeout_still_checks_current_state(self):
        dialog = {"title": "Open"}
        d = make_driver(_find_file_dialog=mock.AsyncMock(return_value=dialog))
        self.assertEqual(d._wait_file_dialog(timeout=0), dialog)
        with self.assertRaisesRegex(RuntimeError, "did not close"):
            d._wait_file_dialog(appear=False, timeout=0)
        d._cua._find_file_dialog.return_value = None
        self.assertIsNone(d._wait_file_dialog(appear=False, timeout=0))
        with self.assertRaisesRegex(RuntimeError, "did not open"):
            d._wait_file_dialog(timeout=0)

    def test_rejects_invalid_timeout_before_discovery(self):
        finder = mock.AsyncMock()
        d = make_driver(_find_file_dialog=finder)
        for timeout in (-1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                d._wait_file_dialog(timeout=timeout)
        finder.assert_not_awaited()

    def test_poll_sleep_is_capped_to_remaining_time(self):
        d = make_driver(_find_file_dialog=mock.AsyncMock(return_value=None))
        clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[0, 0.8, 1]), sleep=mock.Mock())
        with mock.patch.object(driver, "time", clock):
            with self.assertRaisesRegex(RuntimeError, "did not open"):
                d._wait_file_dialog(timeout=1)
        self.assertAlmostEqual(clock.sleep.call_args.args[0], 0.2)


class ChooseFileTests(unittest.TestCase):
    def test_windows_dialog_uses_accessibility_or_keyboard_fallback(self):
        for native in (True, False):
            with self.subTest(native=native):
                d = make_driver(
                    capture=mock.AsyncMock(return_value=SimpleNamespace(
                        content=[SimpleNamespace(type="image", data="PNG")],
                    )),
                    _last_elements=[
                        elem(1, "File name:", role="edit"),
                        elem(2, "Open", role="button", actions=["invoke"], frame={"w": 5, "h": 5}),
                        elem(3, "Open", role="splitbutton", actions=["invoke"], frame={"w": 20, "h": 10}),
                    ],
                    _tools={"set_value": None} if native else {},
                    set_value=mock.AsyncMock(), click=mock.AsyncMock(),
                    type_text=mock.AsyncMock(), key=mock.AsyncMock(),
                )
                dialog = {"title": "Open", "app_name": "Demo"}
                with mock.patch.object(d, "_wait_file_dialog", side_effect=[dialog, None]):
                    d._choose_file_in_dialog("C:/protocol.py")
                if native:
                    d._cua.set_value.assert_awaited_once_with(1, "C:/protocol.py")
                    d._cua.click.assert_awaited_once_with(element=3)
                    d._cua.type_text.assert_not_awaited()
                else:
                    d._cua.type_text.assert_awaited_once_with("C:/protocol.py", delivery_mode="foreground")
                    d._cua.key.assert_awaited_once_with("return", delivery_mode="foreground")
                    d._cua.click.assert_not_awaited()

    def test_portal_dialog_gets_ctrl_l_path_enter(self):
        dialog = {"title": "Open Files", "app_name": "xdg-desktop-portal-gnome",
                  "pid": 10, "window_id": 20}
        sent, state = [], {"open": True}

        async def matching(app):
            return [{"title": "Demo", "app_name": "Demo"}]

        async def find(app=""):
            return dialog if state["open"] else None

        async def call(tool, args):
            sent.append((tool, args))

        async def key(keys, **kw):
            sent.append(("key", keys))
            if keys == "return":
                state["open"] = False

        async def type_text(text, **kw):
            sent.append(("type", text))

        d = make_driver(_matching_windows=matching, _find_file_dialog=find, _call=call,
                        key=key, type_text=type_text, _pid=None, _window_id=None)
        with mock.patch.object(driver.time, "sleep"):
            d._choose_file_in_dialog("/tmp/protocol.py")

        self.assertEqual([s for s in sent if s[0] in {"key", "type"}],
                         [("key", "ctrl+l"), ("type", "/tmp/protocol.py"), ("key", "return")])
        # input was re-targeted at the dialog window
        self.assertEqual((d._cua._pid, d._cua._window_id), (10, 20))

    def test_raises_when_dialog_never_opens(self):
        async def matching(app):
            return [{"title": "Demo", "app_name": "Demo"}]

        async def find(app=""):
            return None

        d = make_driver(_matching_windows=matching, _find_file_dialog=find)
        with mock.patch.object(driver.time, "sleep"):
            with self.assertRaises(RuntimeError):
                d._choose_file_in_dialog("/tmp/x.py", timeout=0)

    def test_raises_when_dialog_does_not_close(self):
        dialog = {"title": "Open Files", "app_name": "xdg-desktop-portal-gnome",
                  "pid": 10, "window_id": 20}

        async def matching(app):
            return [{"title": "Demo", "app_name": "Demo"}]

        async def find(app=""):
            return dialog  # stays open forever

        async def noop(*a, **k):
            return None

        d = make_driver(_matching_windows=matching, _find_file_dialog=find, _call=noop,
                        key=noop, type_text=noop, _pid=None, _window_id=None)
        with mock.patch.object(driver.time, "sleep"):
            with self.assertRaises(RuntimeError):
                d._choose_file_in_dialog("/tmp/x.py", timeout=0)


class StartupEnvTests(unittest.TestCase):
    def test_display_variables_are_forwarded_to_cua_driver(self):
        captured = {}

        class FakeCtx:
            async def __aenter__(self):
                return object(), object()

            async def __aexit__(self, *a):
                return None

        class FakeSession:
            def __init__(self, *a):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def initialize(self):
                return None

            async def list_tools(self):
                return SimpleNamespace(tools=[])

        def fake_stdio_client(params, **kw):
            captured["env"] = params.env
            return FakeCtx()

        env = {"DISPLAY": ":7", "XAUTHORITY": "/tmp/xauth", "XDG_RUNTIME_DIR": "/run/user/9"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(cua_client.shutil, "which", return_value="/usr/bin/cua-driver"), \
             mock.patch.object(cua_client, "stdio_client", fake_stdio_client), \
             mock.patch.object(cua_client, "ClientSession", FakeSession):
            asyncio.run(CuaDriverClient().start())

        self.assertEqual(captured["env"]["DISPLAY"], ":7")
        self.assertEqual(captured["env"]["XAUTHORITY"], "/tmp/xauth")
        self.assertEqual(captured["env"]["PYTHONUTF8"], "1")

    def test_missing_cua_driver_gives_clear_error(self):
        with mock.patch.object(cua_client.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "cua-driver not found"):
                asyncio.run(CuaDriverClient().start())


class CommandSurfaceTests(unittest.TestCase):
    def test_capture_optionally_returns_independent_elements_from_one_capture(self):
        elements = [elem(1, "Start", frame={"x": 10})]
        capture = mock.AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="image", data="PNG")],
        ))
        d = make_driver(capture=capture, _last_elements=elements)
        result = d.capture_screenshot(include_elements=True)
        self.assertEqual(result["image_b64"], "PNG")
        self.assertEqual(result["elements"], elements)
        capture.assert_awaited_once_with(app="Demo", include_elements=True)
        result["elements"][0]["frame"]["x"] = 99
        self.assertEqual(elements[0]["frame"]["x"], 10)
        self.assertNotIn("elements", d.capture_screenshot())
        self.assertFalse(hasattr(d, "get_elements"))

    def test_only_explicit_machine_commands_are_remote(self):
        self.assertEqual(
            resolve_command_names(object.__new__(Driver)),
            {"capture_screenshot", "import_file"},
        )
        self.assertEqual(resolve_command_names(object.__new__(GuiDriver)), set())
        for name in ("find_element", "wait_for_element", "click_element", "set_value",
                     "type_text", "scroll", "press_key", "click_at"):
            with self.subTest(helper=name):
                self.assertIs(getattr(Driver, name), getattr(GuiDriver, name))

    def test_example_import_composes_gui_operations(self):
        d = make_driver()
        calls = mock.Mock()
        d.click_element = calls.click
        d._choose_file_in_dialog = calls.choose
        d.capture_screenshot = calls.capture
        d.find_element = calls.find
        calls.find.side_effect = ElementNotFoundError("absent")
        d.wait_for_element = calls.wait
        calls.wait.return_value = {"label": "Loaded method.xml"}
        self.assertEqual(d.import_file("/tmp/method.xml", "Loaded method.xml"), {
            "imported_file": "/tmp/method.xml",
        })
        self.assertEqual(calls.mock_calls, [
            mock.call.find("Loaded method.xml", ""), mock.call.click("Import"),
            mock.call.wait("Browse", timeout=30.0, error_labels=["Import failed"]),
            mock.call.click("Browse"), mock.call.choose("/tmp/method.xml", timeout=30.0),
            mock.call.wait("Loaded method.xml", "", timeout=30.0, error_labels=["Import failed"]),
        ])
        calls.reset_mock()
        with self.assertRaises(ValueError):
            d.import_file("  ", "Loaded method.xml")
        self.assertEqual(calls.mock_calls, [])

    def test_import_rejects_stale_success_and_does_not_hide_timeout(self):
        d = make_driver()
        d.find_element = mock.Mock(return_value={"label": "Loaded"})
        d.click_element = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            d.import_file("/tmp/file", "Loaded")
        d.click_element.assert_not_called()
        d.find_element.side_effect = ElementNotFoundError("absent")
        d.wait_for_element = mock.Mock(side_effect=[{}, TimeoutError("not loaded")])
        d._choose_file_in_dialog = mock.Mock()
        d.capture_screenshot = mock.Mock()
        with self.assertRaisesRegex(TimeoutError, "not loaded"):
            d.import_file("/tmp/file", "Loaded")
        d.capture_screenshot.assert_not_called()

    def test_capture_returns_screenshot_without_caching_image(self):
        async def capture(app=None, include_elements=False):
            return SimpleNamespace(content=[SimpleNamespace(type="image", data="PNGDATA")])

        d = make_driver(capture=capture)
        result = d.capture_screenshot()
        self.assertEqual(result["image_b64"], "PNGDATA")
        self.assertNotIn("image_b64", d._snapshot()["last_status"])  # state stays small


if __name__ == "__main__":
    unittest.main()
