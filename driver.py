"""PUDA GUI driver template — controls any desktop instrument software via Hermes computer use.

Works on Windows and Linux (X11). No vendor SDK and no LLM: the driver talks to the
target application through `cua-driver` (MCP), using the accessibility tree where
possible and raw screenshots otherwise.

To deploy a new machine, copy this template and add the machine's own commands in
the "MACHINE-SPECIFIC COMMANDS" section of `GuiDriver` (see README.md).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import threading
import time
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from puda import command

logger = logging.getLogger(__name__)

IS_LINUX = sys.platform.startswith("linux")

# Titles of the native file-chooser dialog. Windows names it "Open"/"Save As";
# the GNOME file chooser (xdg-desktop-portal) names it "Open Files".
_FILE_DIALOG_TITLES = {"open", "open file", "open files", "save as"}

# Display-session variables that cua-driver needs on Linux. The MCP stdio client
# forwards only a small allowlist of environment variables to the child process,
# so these must be passed explicitly or cua-driver cannot see any windows.
_DISPLAY_ENV_VARS = (
    "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS",
)


# ─────────────────────────────────────────────────────────────────────────────
# cua-driver MCP client  (generic — no machine-specific code)
# ─────────────────────────────────────────────────────────────────────────────

class CuaDriverClient:
    """
    Async MCP client for cua-driver.

    Starts cua-driver as a stdio subprocess, discovers its MCP tools, then
    exposes capture / click / type / key / scroll behind a stable async API.
    Tool-name resolution tries the Hermes wrapper name first, then native
    cua-driver names, so the client works across driver versions.
    """

    _CAPTURE_TOOLS = ["computer_use", "screenshot", "capture_screen", "get_window_state"]
    _CLICK_TOOLS   = ["computer_use", "click", "left_click"]
    _TYPE_TOOLS    = ["computer_use", "type", "type_text"]
    _KEY_TOOLS     = ["computer_use", "key", "press_key", "hotkey"]
    _SCROLL_TOOLS  = ["computer_use", "scroll", "scroll_element"]

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._ctx = None
        self._tools: dict[str, Any] = {}
        self._pid: int | None = None
        self._window_id: int | None = None
        self._snapshot_id: str | None = None
        self._last_elements: list[dict[str, Any]] = []
        self._capture_meta: dict[str, Any] = {}

    async def start(self) -> None:
        """Start the cua-driver MCP subprocess and initialise the session."""
        if not shutil.which("cua-driver"):
            raise RuntimeError(
                "cua-driver not found on PATH.\n"
                "Install it via Hermes Agent:\n"
                "  1. Download Hermes: https://hermes-agent.nousresearch.com\n"
                "  2. Run: hermes computer-use install\n"
                "  3. Verify: hermes computer-use doctor\n"
                "Or set HERMES_CUA_DRIVER_CMD to the full path of your cua-driver binary."
            )
        # Force UTF-8 in the Python-based Windows child process, and forward the
        # display-session variables that Linux needs (see _DISPLAY_ENV_VARS).
        child_env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        for key in _DISPLAY_ENV_VARS:
            if os.environ.get(key):
                child_env[key] = os.environ[key]
        params = StdioServerParameters(
            command="cua-driver", args=["mcp"],
            env=child_env,
            encoding="utf-8", encoding_error_handler="replace",
        )
        # Pass stderr as bytes. On Windows, a text stderr stream uses the active
        # ANSI code page (often cp1252); cua-driver may emit UTF-8 bytes that are
        # invalid in that code page and crash subprocess._readerthread.
        self._ctx = stdio_client(params, errlog=getattr(sys.stderr, "buffer", sys.stderr))
        read, write = await self._ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        tools_result = await self._session.list_tools()
        self._tools = {t.name: t for t in tools_result.tools}
        logger.info("cua-driver connected. Tools: %s", sorted(self._tools.keys()))

    async def stop(self) -> None:
        """Gracefully close the MCP session and the cua-driver subprocess."""
        for obj in (self._session, self._ctx):
            if obj:
                try:
                    await obj.__aexit__(None, None, None)
                except Exception:
                    pass

    # ── tool resolution helpers ────────────────────────────────────────────

    def _resolve(self, candidates: list[str]) -> str:
        for name in candidates:
            if name in self._tools:
                return name
        raise RuntimeError(
            f"None of {candidates} found in cua-driver. Available: {sorted(self._tools.keys())}"
        )

    async def _call(self, tool: str, args: dict) -> Any:
        assert self._session, "CuaDriverClient not started"
        return await self._session.call_tool(tool, args)

    @staticmethod
    def _structured(result: Any) -> dict[str, Any]:
        value = getattr(result, "structuredContent", None)
        if value is None:
            value = getattr(result, "structured_content", None)
        return value if isinstance(value, dict) else {}

    # ── window discovery ───────────────────────────────────────────────────

    async def _matching_windows(self, app: str) -> list[dict[str, Any]]:
        # Include minimized/off-screen windows so they can be restored onto the
        # user's primary display instead of being controlled invisibly.
        result = await self._call("list_windows", {"on_screen_only": False})
        windows = self._structured(result).get("windows", [])
        needle = app.casefold()
        matches = [
            window for window in windows
            if (needle in str(window.get("app_name", "")).casefold()
                or needle in str(window.get("title", "")).casefold())
        ]
        if not matches:
            raise RuntimeError(
                f"No window matching {app!r}. "
                "Open the application in the logged-in desktop session "
                "(on Linux it must run under an X11 session, not Wayland)."
            )
        return matches

    async def _find_file_dialog(self, app: str = "") -> dict[str, Any] | None:
        """Return the native file-chooser window, if one is open.

        Windows lists it under the target app with the title "Open". On Linux the
        GNOME file chooser is owned by xdg-desktop-portal-* and titled "Open
        Files", so it must be found among *all* windows rather than by app name.
        """
        needle = app.casefold()
        result = await self._call("list_windows", {"on_screen_only": False})
        for window in self._structured(result).get("windows", []):
            title = str(window.get("title", "")).casefold()
            owner = str(window.get("app_name", "")).casefold()
            if title in _FILE_DIALOG_TITLES and (
                "portal" in owner or not owner or (needle and needle in owner)
            ):
                return window
        return None

    async def _make_window_visible(self, window: dict[str, Any]) -> dict[str, Any]:
        """Restore an exact window to the primary display and foreground it."""
        required = {"get_screen_size", "set_window_frame", "bring_to_front"}
        missing = required - self._tools.keys()
        if missing:
            raise RuntimeError(
                "Visible foreground control is unavailable; cua-driver is missing "
                + ", ".join(sorted(missing))
            )

        pid = int(window["pid"])
        window_id = int(window["window_id"])
        bounds = window.get("bounds", {})
        screen = self._structured(await self._call("get_screen_size", {}))
        screen_width = int(screen.get("width", 0))
        screen_height = int(screen.get("height", 0))
        if not screen_width or not screen_height:
            raise RuntimeError("Could not determine the primary display size")

        x = int(bounds.get("x", 0))
        y = int(bounds.get("y", 0))
        width = int(bounds.get("width", 0))
        height = int(bounds.get("height", 0))
        intersects_primary = (
            width > 0 and height > 0 and x < screen_width and y < screen_height
            and x + width > 0 and y + height > 0
        )
        needs_reposition = (
            window.get("minimized", False)
            or not window.get("is_on_screen", False)
            or not intersects_primary
        )
        # Windows cannot resize a minimized HWND. Restore/activate it first,
        # then place it on the primary display and reaffirm foreground focus.
        await self._call("bring_to_front", {"pid": pid, "window_id": window_id})
        if needs_reposition:
            width = min(max(width, 1000), screen_width)
            height = min(max(height, 700), screen_height)
            await self._call("set_window_frame", {
                "pid": pid, "window_id": window_id, "x": 0, "y": 0,
                "width": width, "height": height,
            })
            await self._call("bring_to_front", {"pid": pid, "window_id": window_id})

        refreshed = self._structured(
            await self._call("list_windows", {"on_screen_only": False})
        ).get("windows", [])
        exact = next(
            (item for item in refreshed if int(item.get("window_id", -1)) == window_id),
            None,
        )
        if not exact or exact.get("minimized", False) or not exact.get("is_on_screen", False):
            raise RuntimeError(
                f"Refusing GUI input: {window.get('title', 'target window')!r} "
                "could not be made visible on the user's screen"
            )
        return exact

    @classmethod
    def _require_input(cls, result: Any) -> Any:
        """Fail closed on rejected/no-op input; unverifiable delivery needs read-back."""
        structured = cls._structured(result)
        if (getattr(result, "is_error", False) or getattr(result, "isError", False)
                or structured.get("ok") is False
                or structured.get("effect") == "suspected_noop"):
            raise RuntimeError(
                f"cua-driver input rejected: {structured or str(getattr(result, 'content', result))}"
            )
        return result

    # ── computer-use actions ───────────────────────────────────────────────

    async def capture(self, app: str | None = None, mode: str = "screenshot") -> Any:
        """Capture a window. mode='som' also refreshes the accessibility element list."""
        tool = self._resolve(self._CAPTURE_TOOLS)
        if tool == "get_window_state":
            if not app:
                raise RuntimeError("An app name is required for window capture")

            # Electron can leave a second blank top-level window behind. Inspect
            # every matching window and use the one with the richest UI tree.
            candidates: list[tuple[int, int, int, Any, dict[str, Any], int, int]] = []
            for window in await self._matching_windows(app):
                window = await self._make_window_visible(window)
                pid = int(window["pid"])
                window_id = int(window["window_id"])
                result = await self._call(tool, {
                    "pid": pid,
                    "window_id": window_id,
                    "include_accessibility_tree": True,
                    "include_screenshot": True,
                })
                structured = self._structured(result)
                score = int(structured.get("total_element_count", 0))
                image_size = len(self.extract_image_b64(result) or "")
                title = str(window.get("title", "")).casefold()
                modal = int(title in _FILE_DIALOG_TITLES or title.startswith("blob:"))
                candidates.append((modal, score, image_size, result, structured, pid, window_id))

            _, _, _, result, structured, self._pid, self._window_id = max(
                candidates, key=lambda item: (item[0], item[1], item[2])
            )
            self._snapshot_id = structured.get("snapshot_id")
            self._last_elements = structured.get("elements", [])
            self._capture_meta = structured
            return result

        args: dict = {"action": "capture", "mode": mode} if tool == "computer_use" else {}
        if app:
            args["app"] = app
        return await self._call(tool, args)

    async def click(self, element: int | None = None, x: int | None = None,
                    y: int | None = None, button: str = "left",
                    delivery_mode: str | None = "foreground") -> Any:
        tool = self._resolve(self._CLICK_TOOLS)
        args: dict = {"action": "click", "button": button} if tool == "computer_use" else {"button": button}
        if tool == "click" and self._pid is not None:
            args["pid"] = self._pid
            args["window_id"] = self._window_id
            if delivery_mode:
                args["delivery_mode"] = delivery_mode
        if element is not None:
            if tool == "click":
                args["element_index"] = element
                args["window_id"] = self._window_id
                if self._snapshot_id:
                    args["snapshot_id"] = self._snapshot_id
            else:
                args["element"] = element
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        return self._require_input(await self._call(tool, args))

    async def type_text(self, text: str, delivery_mode: str | None = "foreground") -> Any:
        tool = self._resolve(self._TYPE_TOOLS)
        args = {"action": "type", "text": text} if tool == "computer_use" else {"text": text}
        if tool == "type_text" and self._pid is not None:
            args.update({"pid": self._pid, "window_id": self._window_id})
            if delivery_mode:
                args["delivery_mode"] = delivery_mode
        return self._require_input(await self._call(tool, args))

    async def set_value(self, element: int, value: str) -> Any:
        """Set a native UIA value using the current captured-window snapshot."""
        if "set_value" not in self._tools:
            raise RuntimeError("cua-driver does not expose set_value")
        args: dict[str, Any] = {
            "pid": self._pid,
            "window_id": self._window_id,
            "element_index": element,
            "value": value,
        }
        if self._snapshot_id:
            args["snapshot_id"] = self._snapshot_id
        return self._require_input(await self._call("set_value", args))

    async def key(self, keys: str, capture_after: bool = False,
                  delivery_mode: str | None = "foreground") -> Any:
        tool = self._resolve(self._KEY_TOOLS)
        if tool == "computer_use":
            args = {"action": "key", "keys": keys, "capture_after": capture_after}
        elif tool == "press_key":
            parts = keys.lower().split("+")
            args = {"key": parts[-1], "modifiers": parts[:-1]}
            if self._pid is not None:
                args.update({"pid": self._pid, "window_id": self._window_id})
                if delivery_mode:
                    args["delivery_mode"] = delivery_mode
        else:
            args = {"keys": keys}
        return self._require_input(await self._call(tool, args))

    async def scroll(self, direction: str = "down", amount: int = 3) -> Any:
        tool = self._resolve(self._SCROLL_TOOLS)
        args = ({"action": "scroll", "direction": direction, "amount": amount}
                if tool == "computer_use" else {"direction": direction, "amount": amount})
        return await self._call(tool, args)

    # ── image extraction ───────────────────────────────────────────────────

    @staticmethod
    def extract_image_b64(result: Any) -> str | None:
        """Extract a base64 PNG from an MCP tool result (image content block)."""
        content = getattr(result, "content", None)
        if not content:
            return None
        for block in content:
            if getattr(block, "type", None) == "image":
                return getattr(block, "data", None)
            if "image" in (getattr(block, "mimeType", None) or ""):
                return getattr(block, "data", None)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# PUDA GUI driver
# ─────────────────────────────────────────────────────────────────────────────

class GuiDriver:
    """
    PUDA GUI driver — controls a desktop instrument application via cua-driver.

    Follows the PUDA 0.1.x edge contract:
      * Only methods marked ``@command`` are remotely callable.
      * Internal helpers, startup, cached state and telemetry use a leading
        underscore (``main.py`` passes ``_snapshot`` to ``EdgeRunner`` as the
        state handler).
      * Screens are returned as raw base64 PNG; no LLM interprets them.

    Platform notes
      * Windows: works from an interactive (RDP / console) session.
      * Linux: the target app and this driver must run in an **X11** session.
        Native Wayland windows are invisible to cua-driver.
    """

    def __init__(self, target_app: str) -> None:
        self.target_app = target_app
        self._last_status: dict = {}
        self._cua = CuaDriverClient()

        # Background asyncio loop bridging sync PUDA worker threads to async MCP calls.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="guidriver-async-loop"
        )
        self._loop_thread.start()

    # ── lifecycle (not remotely callable) ──────────────────────────────────

    def _startup(self) -> None:
        """Start the cua-driver MCP session. Called once from main.py."""
        self._run(self._cua.start())
        logger.info("GuiDriver ready — target_app=%r", self.target_app)

    def _run(self, coro) -> Any:
        """Submit an async coroutine to the background loop and block until done."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60)

    def _snapshot(self) -> dict:
        """Cached state merged into every MACHINE_STATE update (must not touch the screen)."""
        return {"target_app": self.target_app, "last_status": self._last_status}

    def reset(self) -> bool:
        """Send Escape to cancel any in-progress operation."""
        self._run(self._cua.key("escape"))
        return True

    def shutdown(self) -> bool:
        """Close the cua-driver MCP session cleanly."""
        try:
            self._run(self._cua.stop())
        except Exception as e:
            logger.warning("shutdown error: %s", e)
        self._loop.call_soon_threadsafe(self._loop.stop)
        return True

    def _stream_status(self) -> dict | None:
        """Raw screenshot telemetry sample (not scheduled by default).

        Every capture brings the window to the foreground, so periodic polling
        steals focus from the operator. To enable it, decorate this method with
        ``@tlm_stream(interval=30.0, name="instrument_status")`` (import it from puda).
        """
        try:
            self._last_status = {"target_app": self.target_app, "image_b64": self._screenshot()}
            return self._last_status
        except Exception as e:
            logger.warning("stream_status failed: %s", e)
            return None

    # ── generic GUI helpers ────────────────────────────────────────────────

    def _screenshot(self, mode: str = "screenshot") -> str:
        """Capture the target window; returns base64 PNG. mode='som' refreshes element list."""
        result = self._run(self._cua.capture(app=self.target_app, mode=mode))
        img = CuaDriverClient.extract_image_b64(result)
        if img is None:
            raise RuntimeError(
                f"cua-driver returned no image for app={self.target_app!r}. "
                "Ensure the app is visible and screen capture is permitted."
            )
        return img

    def _som_click(self, description: str) -> int:
        """Click the accessibility element whose label/role/value best matches ``description``.

        Raises RuntimeError if nothing matches. Prefer this over pixel clicks: it
        does not depend on screen resolution or window position.
        """
        wanted = set(re.findall(r"[a-z0-9]+", description.casefold()))
        ignored = {"a", "an", "the", "in", "on", "to", "of", "for", "button", "option"}
        wanted -= ignored

        # Some apps (e.g. Electron) can temporarily expose only a document root.
        # Retry fresh snapshots before giving up.
        for attempt in range(3):
            self._screenshot(mode="som")
            candidates: list[tuple[int, int, str]] = []
            for element in self._cua._last_elements:
                label = " ".join(str(element.get(key, "")) for key in ("label", "role", "value"))
                words = set(re.findall(r"[a-z0-9]+", label.casefold()))
                score = len(wanted & words)
                if score and element.get("enabled", True):
                    candidates.append((score, int(element["element_index"]), label))
            if candidates:
                _, elem, label = max(candidates, key=lambda item: (item[0], -item[1]))
                selected = next(e for e in self._cua._last_elements if int(e["element_index"]) == elem)
                frame = selected.get("frame", {})
                window = self._cua._capture_meta.get("window_bounds", {})
                if frame and window:
                    cx = frame["x"] + frame["w"] / 2
                    cy = frame["y"] + frame["h"] / 2
                    if not (window["x"] <= cx < window["x"] + window["width"]
                            and window["y"] <= cy < window["y"] + window["height"]):
                        raise RuntimeError(
                            f"Accessibility coordinates for {description!r} are outside the captured window. "
                            "Use the click_at command with coordinates taken from a fresh screenshot."
                        )
                logger.info("accessibility click: element %d (%s) for %r", elem, label, description)
                self._run(self._cua.click(element=elem))
                return elem
            if attempt < 2:
                time.sleep(0.25)
        raise RuntimeError(
            f"No accessible UI element matched {description!r}. "
            "Capture the screen and use an explicit element or coordinate action."
        )

    def _element_labels(self) -> list[str]:
        """Labels of the elements from the latest ``mode='som'`` capture (for debugging)."""
        return [str(e.get("label", "")) for e in self._cua._last_elements if e.get("label")]

    # ── native file-dialog helpers ─────────────────────────────────────────

    def _current_file_dialog(self) -> dict | None:
        """Return the open native file dialog, or None.

        First a window of the target app titled "Open"/"Save As" (how Windows
        exposes it); then a search across all windows for the GNOME portal
        chooser ("Open Files"), which belongs to xdg-desktop-portal, not the app.
        """
        windows = self._run(self._cua._matching_windows(self.target_app))
        for window in windows:
            if str(window.get("title", "")).casefold() in _FILE_DIALOG_TITLES:
                return window
        finder = getattr(self._cua, "_find_file_dialog", None)
        return self._run(finder(self.target_app)) if finder is not None else None

    def _wait_file_dialog(self, appear: bool = True, timeout: float = 15.0) -> dict | None:
        """Poll until the file dialog appears (or disappears if ``appear=False``).

        Apps repaint slowly, so a generous timeout avoids false failures.
        """
        deadline = time.monotonic() + timeout
        while True:
            dialog = self._current_file_dialog()
            if appear and dialog is not None:
                return dialog
            if not appear and dialog is None:
                return None
            if time.monotonic() >= deadline:
                return dialog
            time.sleep(0.5)

    def _type_into_portal_dialog(self, dialog: dict, file_path: str) -> None:
        """Type a path into the GNOME/GTK file chooser: Ctrl+L, path, Enter."""
        # Re-target the next input at the dialog window, not the app window.
        self._cua._pid = int(dialog["pid"])
        self._cua._window_id = int(dialog["window_id"])
        self._run(self._cua._call("bring_to_front", {
            "pid": self._cua._pid, "window_id": self._cua._window_id,
        }))
        time.sleep(1.0)
        self._run(self._cua.key("ctrl+l", delivery_mode="foreground"))
        time.sleep(0.5)
        self._run(self._cua.type_text(file_path, delivery_mode="foreground"))
        time.sleep(0.5)
        self._run(self._cua.key("return", delivery_mode="foreground"))

    def _choose_file_in_dialog(self, file_path: str, timeout: float = 15.0) -> None:
        """Fill the native file dialog with ``file_path`` and confirm it.

        Call this *after* your machine-specific code has clicked whatever opens the
        dialog (Import / Browse / Open...). Works with the Windows common dialog and
        the GNOME file chooser. Raises RuntimeError if the dialog never opens or
        does not close afterwards.
        """
        dialog = self._wait_file_dialog(timeout=timeout)
        if dialog is None:
            raise RuntimeError(
                "The file dialog did not open. Open it first (click the app's "
                "Import/Browse/Open button), then call _choose_file_in_dialog."
            )

        if "portal" in str(dialog.get("app_name", "")).casefold():
            self._type_into_portal_dialog(dialog, file_path)
        else:
            self._screenshot()  # select the modal dialog before sending input
            elements = getattr(self._cua, "_last_elements", [])
            filename_fields = [
                e for e in elements
                if str(e.get("role", "")).casefold() == "edit"
                and str(e.get("label", "")).casefold() == "file name:"
            ]
            open_buttons = [
                e for e in elements
                if str(e.get("role", "")).casefold() in {"button", "splitbutton"}
                and str(e.get("label", "")).casefold() == "open"
                and "invoke" in e.get("actions", [])
            ]
            if filename_fields and open_buttons and "set_value" in getattr(self._cua, "_tools", {}):
                self._run(self._cua.set_value(int(filename_fields[0]["element_index"]), file_path))
                open_button = max(
                    open_buttons,
                    key=lambda e: e.get("frame", {}).get("w", 0) * e.get("frame", {}).get("h", 0),
                )
                self._run(self._cua.click(element=int(open_button["element_index"])))
            else:
                # Fallback for older cua-driver versions: foreground keystrokes.
                self._run(self._cua.type_text(file_path, delivery_mode="foreground"))
                self._run(self._cua.key("return", delivery_mode="foreground"))

        if self._wait_file_dialog(appear=False, timeout=timeout) is not None:
            raise RuntimeError("The file dialog did not close after choosing the file")

    # ── base PUDA commands (generic — work for any application) ────────────

    @command
    def status(self) -> dict:
        """Return a raw base64 PNG screenshot of the target application (no interpretation)."""
        self._last_status = {"target_app": self.target_app, "captured_at": time.time()}
        return {"target_app": self.target_app, "image_b64": self._screenshot()}

    @command
    def capture_screenshot(self) -> dict:
        """Return a raw base64 PNG screenshot of the target application."""
        return {"app": self.target_app, "image_b64": self._screenshot()}

    @command
    def ask_screen(self, question: str) -> dict:
        """Capture the screen for an external reader; the driver does not interpret it."""
        return {"question": question, "app": self.target_app, "image_b64": self._screenshot()}

    @command
    def find_and_click(self, description: str) -> dict:
        """
        Locate and click an accessibility element by description.

        Args:
            description: Words from the element's label, e.g. "Start run" or "Save".

        Returns:
            dict: {'clicked_element': int, 'description': str}
        """
        logger.info("find_and_click: %r", description)
        elem = self._som_click(description)
        return {"clicked_element": elem, "description": description}

    @command
    def type_text(self, text: str, field_description: str = "") -> dict:
        """
        Type text, optionally clicking a field first.

        Args:
            text: Text to type.
            field_description: Optional field label to click before typing.
                               Leave empty to type into whatever is focused.

        Returns:
            dict: {'typed': str, 'field': str}
        """
        if field_description:
            self._som_click(field_description)
        logger.info("type_text: %r into %r", text, field_description or "<focused>")
        self._run(self._cua.type_text(text))
        return {"typed": text, "field": field_description}

    @command
    def press_key(self, keys: str) -> dict:
        """
        Press a keyboard shortcut in the target application.

        Args:
            keys: Key combination, e.g. 'escape', 'ctrl+s', 'f5'.

        Returns:
            dict: {'keys': str}
        """
        self._run(self._cua.key(keys))
        return {"keys": keys}

    @command
    def click_at(self, x: int, y: int) -> dict:
        """Dispatch a visible foreground click using fresh screenshot pixels.

        Use only coordinates grounded in a fresh screenshot, not accessibility
        bounds. The target window is restored to the primary display and
        foregrounded. No automatic repeat is performed.
        """
        self._screenshot()
        width = self._cua._capture_meta.get("screenshot_width")
        height = self._cua._capture_meta.get("screenshot_height")
        if width is None or height is None or not (0 <= x < width and 0 <= y < height):
            raise ValueError("Click coordinates must be inside the fresh screenshot")
        result = self._run(self._cua.click(x=x, y=y, delivery_mode="foreground"))
        time.sleep(0.5)
        return {"x": x, "y": y,
                "effect": CuaDriverClient._structured(result).get("effect", "unverifiable"),
                "image_b64": self._screenshot()}

    # ══════════════════════════════════════════════════════════════════════
    # MACHINE-SPECIFIC COMMANDS — add this machine's commands below.
    #
    # Build them from the helpers above:
    #   self._som_click("Start run")          click by accessibility label
    #   self._cua.key / type_text / click     raw input (via self._run(...))
    #   self._choose_file_in_dialog(path)     fill a native file dialog
    #   self._screenshot()                    raw base64 PNG of the window
    #
    # Example:
    #
    #   @command
    #   def start_run(self) -> dict:
    #       """Click Start and return the resulting screen."""
    #       self._som_click("Start run")
    #       time.sleep(1.0)
    #       return {"image_b64": self._screenshot()}
    #
    # Rules: only @command methods are remotely callable; give each a docstring
    # and type hints (they are published in the command catalog); return only
    # JSON-serialisable values.
    # ══════════════════════════════════════════════════════════════════════
