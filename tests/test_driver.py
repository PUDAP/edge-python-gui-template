"""Offline tests for the generic GUI driver logic (no display or cua-driver needed)."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver  # noqa: E402
from driver import CuaDriverClient, GuiDriver  # noqa: E402


def make_driver(**cua_attrs) -> GuiDriver:
    """A GuiDriver without a real event loop / cua-driver; coroutines run inline."""
    d = object.__new__(GuiDriver)
    d.target_app = "Demo"
    d._last_status = {}
    d._run = lambda coro: asyncio.run(coro)
    d._cua = SimpleNamespace(**cua_attrs)
    return d


def elem(index, label, role="push button", **extra):
    return {"element_index": index, "label": label, "role": role, **extra}


class SomClickTests(unittest.TestCase):
    def _driver(self, elements):
        clicked = []

        async def capture(app=None, mode="screenshot"):
            return SimpleNamespace(content=[SimpleNamespace(type="image", data="AAAA")])

        async def click(element=None, **kw):
            clicked.append(element)

        d = make_driver(capture=capture, click=click, _last_elements=elements, _capture_meta={})
        return d, clicked

    def test_clicks_best_matching_element(self):
        d, clicked = self._driver([elem(1, "Cancel run"), elem(2, "Start run"), elem(3, "Start")])
        self.assertEqual(d._som_click("Start run button"), 2)
        self.assertEqual(clicked, [2])

    def test_raises_when_nothing_matches(self):
        d, clicked = self._driver([elem(1, "Cancel")])
        with mock.patch.object(driver.time, "sleep"):
            with self.assertRaises(RuntimeError):
                d._som_click("Save")
        self.assertEqual(clicked, [])

    def test_ignores_disabled_elements(self):
        d, clicked = self._driver([elem(1, "Start run", enabled=False)])
        with mock.patch.object(driver.time, "sleep"):
            with self.assertRaises(RuntimeError):
                d._som_click("Start run")


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


class ChooseFileTests(unittest.TestCase):
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
             mock.patch.object(driver.shutil, "which", return_value="/usr/bin/cua-driver"), \
             mock.patch.object(driver, "stdio_client", fake_stdio_client), \
             mock.patch.object(driver, "ClientSession", FakeSession):
            asyncio.run(CuaDriverClient().start())

        self.assertEqual(captured["env"]["DISPLAY"], ":7")
        self.assertEqual(captured["env"]["XAUTHORITY"], "/tmp/xauth")
        self.assertEqual(captured["env"]["PYTHONUTF8"], "1")

    def test_missing_cua_driver_gives_clear_error(self):
        with mock.patch.object(driver.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "cua-driver not found"):
                asyncio.run(CuaDriverClient().start())


class CommandSurfaceTests(unittest.TestCase):
    def test_internal_helpers_are_not_public(self):
        public = {n for n in dir(GuiDriver) if not n.startswith("_")}
        # reset/shutdown are lifecycle helpers; everything else public is a command
        self.assertEqual(
            public - {"reset", "shutdown"},
            {"status", "capture_screenshot", "ask_screen", "find_and_click",
             "type_text", "press_key", "click_at"},
        )

    def test_status_returns_screenshot_without_llm(self):
        async def capture(app=None, mode="screenshot"):
            return SimpleNamespace(content=[SimpleNamespace(type="image", data="PNGDATA")])

        d = make_driver(capture=capture)
        result = d.status()
        self.assertEqual(result["image_b64"], "PNGDATA")
        self.assertNotIn("image_b64", d._snapshot()["last_status"])  # state stays small


if __name__ == "__main__":
    unittest.main()
