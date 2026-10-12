"""Async cua-driver MCP client for desktop capture and input."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from typing import Any
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

# Titles of the native file-chooser dialog. Windows names it "Open"/"Save As";
# the GNOME file chooser (xdg-desktop-portal) names it "Open Files".
FILE_DIALOG_TITLES = {"open", "open file", "open files", "save as"}

# Display-session variables that cua-driver needs on Linux. The MCP stdio client
# forwards only a small allowlist of environment variables to the child process,
# so these must be passed explicitly or cua-driver cannot see any windows.
_DISPLAY_ENV_VARS = (
    "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS",
)


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
        self._coordinate_capture: dict[str, Any] | None = None

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
        self._coordinate_capture = None
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
            if title in FILE_DIALOG_TITLES and (
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

    def _input_target(self, delivery_mode: str | None) -> dict[str, Any]:
        """Target native input at the captured window when one is available."""
        if self._pid is None:
            return {}
        args: dict[str, Any] = {"pid": self._pid, "window_id": self._window_id}
        if delivery_mode:
            args["delivery_mode"] = delivery_mode
        return args

    async def capture(self, app: str | None = None, mode: str = "screenshot") -> Any:
        """Capture a window. mode='som' also refreshes the accessibility element list."""
        self._coordinate_capture = None
        self._snapshot_id = None
        self._last_elements = []
        self._capture_meta = {}
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
                modal = int(title in FILE_DIALOG_TITLES or title.startswith("blob:"))
                candidates.append((modal, score, image_size, result, structured, pid, window_id))

            _, _, _, result, structured, self._pid, self._window_id = max(
                candidates, key=lambda item: (item[0], item[1], item[2])
            )
            self._snapshot_id = structured.get("snapshot_id")
            self._last_elements = structured.get("elements", [])
            self._capture_meta = structured
            bounds = structured.get("window_bounds", {})
            width = structured.get("screenshot_width")
            height = structured.get("screenshot_height")
            if (self.extract_image_b64(result)
                    and isinstance(width, int) and width > 0
                    and isinstance(height, int) and height > 0
                    and all(key in bounds for key in ("x", "y", "width", "height"))
                    and {"click", "list_windows", "bring_to_front"} <= self._tools.keys()):
                self._coordinate_capture = {
                    "capture_id": uuid4().hex,
                    "pid": self._pid, "window_id": self._window_id,
                    "bounds": {key: bounds[key] for key in ("x", "y", "width", "height")},
                    "width": width, "height": height,
                }
            return result

        args: dict = {"action": "capture", "mode": mode} if tool == "computer_use" else {}
        if app:
            args["app"] = app
        return await self._call(tool, args)

    async def _validate_capture_window(self, capture: dict[str, Any]) -> None:
        """Check the exact captured window without moving or capturing it."""
        result = await self._call("list_windows", {"on_screen_only": False})
        windows = self._structured(result).get("windows", [])
        matches = [window for window in windows
                   if window.get("pid") == capture["pid"]
                   and window.get("window_id") == capture["window_id"]]
        if len(matches) != 1:
            raise RuntimeError("Captured window is unavailable; capture a new screenshot")
        window = matches[0]
        bounds = window.get("bounds", {})
        if (window.get("minimized", False) or not window.get("is_on_screen", False)
                or any(bounds.get(key) != value for key, value in capture["bounds"].items())):
            raise RuntimeError("Captured window moved, resized, or became hidden; capture again")

    async def click_at(self, x: int, y: int, capture_id: str) -> Any:
        """Consume a capture ID and click its unchanged window, without recapturing."""
        capture = self._coordinate_capture
        if not capture_id or capture is None or capture_id != capture["capture_id"]:
            raise RuntimeError("Unknown or stale capture_id; capture a new screenshot")
        if (type(x) is not int or type(y) is not int
                or not (0 <= x < capture["width"] and 0 <= y < capture["height"])):
            raise ValueError("Click coordinates must be inside the referenced screenshot")
        # Consume before awaiting so a repeated or concurrent call cannot reuse it.
        self._coordinate_capture = None
        await self._validate_capture_window(capture)
        target = {"pid": capture["pid"], "window_id": capture["window_id"]}
        self._require_input(await self._call("bring_to_front", target))
        await self._validate_capture_window(capture)
        return self._require_input(await self._call("click", {
            **target, "x": x, "y": y, "button": "left", "delivery_mode": "foreground",
        }))

    async def click(self, element: int | None = None, x: int | None = None,
                    y: int | None = None, button: str = "left",
                    delivery_mode: str | None = "foreground") -> Any:
        self._coordinate_capture = None
        tool = self._resolve(self._CLICK_TOOLS)
        args: dict = {"action": "click", "button": button} if tool == "computer_use" else {"button": button}
        if tool == "click":
            args.update(self._input_target(delivery_mode))
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
        self._coordinate_capture = None
        tool = self._resolve(self._TYPE_TOOLS)
        args = {"action": "type", "text": text} if tool == "computer_use" else {"text": text}
        if tool == "type_text":
            args.update(self._input_target(delivery_mode))
        return self._require_input(await self._call(tool, args))

    async def set_value(self, element: int, value: str) -> Any:
        """Set a native UIA value using the current captured-window snapshot."""
        self._coordinate_capture = None
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
        self._coordinate_capture = None
        tool = self._resolve(self._KEY_TOOLS)
        if tool == "computer_use":
            args = {"action": "key", "keys": keys, "capture_after": capture_after}
        elif tool == "press_key":
            parts = keys.lower().split("+")
            args = {"key": parts[-1], "modifiers": parts[:-1]}
            args.update(self._input_target(delivery_mode))
        else:
            args = {"keys": keys}
        return self._require_input(await self._call(tool, args))

    async def scroll(self, direction: str = "down", amount: int = 3) -> Any:
        self._coordinate_capture = None
        tool = self._resolve(self._SCROLL_TOOLS)
        args = ({"action": "scroll", "direction": direction, "amount": amount}
                if tool == "computer_use" else {"direction": direction, "amount": amount})
        return self._require_input(await self._call(tool, args))

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
