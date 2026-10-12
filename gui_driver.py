"""Reusable PUDA GUI automation base built on the cua-driver MCP client.

Works on Windows and Linux (X11). No vendor SDK and no LLM: the driver talks to the
target application through `cua-driver` (MCP), using the accessibility tree where
possible and raw screenshots otherwise.

Machine-specific commands belong in driver.py, whose Driver subclasses GuiDriver.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from puda import command

from cua_client import CuaDriverClient, FILE_DIALOG_TITLES

logger = logging.getLogger(__name__)



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
            result = self.capture_screenshot()
            return {"image_b64": result["image_b64"]}
        except Exception as e:
            logger.warning("stream_status failed: %s", e)
            return None

    # ── generic GUI helpers ────────────────────────────────────────────────

    def _screenshot(self, include_elements: bool = False) -> str:
        """Return a base64 PNG; optionally refresh accessibility elements as well."""
        result = self._run(self._cua.capture(
            app=self.target_app, include_elements=include_elements,
        ))
        img = CuaDriverClient.extract_image_b64(result)
        if img is None:
            raise RuntimeError(
                f"cua-driver returned no image for app={self.target_app!r}. "
                "Ensure the app is visible and screen capture is permitted."
            )
        return img

    def _find_element(self, label: str, role: str = "") -> dict[str, Any]:
        """Find one enabled element by exact, case-sensitive label and optional role."""
        if not label:
            raise ValueError("An exact non-empty element label is required")
        self._screenshot(include_elements=True)
        matches = [
            element for element in self._cua._last_elements
            if element.get("label") == label
            and (not role or element.get("role") == role)
            and element.get("enabled", True)
        ]
        if not matches:
            raise RuntimeError(f"No enabled element matches label={label!r}, role={role!r}")
        if len(matches) > 1:
            raise RuntimeError(
                f"Ambiguous element: {len(matches)} matches for label={label!r}, role={role!r}. "
                "Inspect get_elements and specify a unique label and role, or use click_at."
            )
        return matches[0]

    def _validate_element_position(self, element: dict[str, Any], description: str) -> None:
        """Reject accessibility coordinates outside the captured window."""
        frame = element.get("frame", {})
        window = self._cua._capture_meta.get("window_bounds", {})
        if not frame or not window:
            return
        cx = frame["x"] + frame["w"] / 2
        cy = frame["y"] + frame["h"] / 2
        if not (window["x"] <= cx < window["x"] + window["width"]
                and window["y"] <= cy < window["y"] + window["height"]):
            raise RuntimeError(
                f"Accessibility coordinates for {description!r} are outside the captured window. "
                "Use the click_at command with coordinates taken from a fresh screenshot."
            )

    def _click_element(self, label: str, role: str = "") -> int:
        """Resolve a unique element from a fresh capture and click it once."""
        selected = self._find_element(label, role)
        self._validate_element_position(selected, label)
        index = int(selected["element_index"])
        self._run(self._cua.click(element=index))
        return index

    def _element_labels(self) -> list[str]:
        """Labels from the latest capture with include_elements=True (for debugging)."""
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
            if str(window.get("title", "")).casefold() in FILE_DIALOG_TITLES:
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

    def _type_into_windows_dialog(self, file_path: str) -> None:
        """Fill the Windows filename field, falling back to foreground typing."""
        self._screenshot(include_elements=True)  # select the modal and inspect its fields
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
            self._run(self._cua.type_text(file_path, delivery_mode="foreground"))
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
            self._type_into_windows_dialog(file_path)

        if self._wait_file_dialog(appear=False, timeout=timeout) is not None:
            raise RuntimeError("The file dialog did not close after choosing the file")

    # ── base PUDA commands (generic — work for any application) ────────────

    @command
    def capture_screenshot(self) -> dict:
        """Return a PNG and a single-use capture_id for coordinate clicks.

        capture_id is None when the backend cannot provide window identity and
        geometry. Any later capture or input invalidates the ID.
        """
        image = self._screenshot()
        self._last_status = {"captured_at": time.time()}
        capture = getattr(self._cua, "_coordinate_capture", None) or {}
        return {"image_b64": image,
                "capture_id": capture.get("capture_id"),
                "width": capture.get("width"), "height": capture.get("height")}

    @command
    def get_elements(self) -> dict:
        """Capture the current accessibility elements, including labels and roles."""
        self._screenshot(include_elements=True)
        return {"elements": self._cua._last_elements}

    @command
    def find_element(self, label: str, role: str = "") -> dict:
        """Find one enabled element using an exact label and optional exact role.

        Matching is case-sensitive. Missing or ambiguous matches raise.
        Element indices describe this capture only; actions resolve a fresh match.
        """
        return dict(self._find_element(label, role))

    @command
    def click_element(self, label: str, role: str = "") -> dict:
        """Click one enabled element by exact label and optional exact role.

        Missing or ambiguous matches raise without sending input.
        """
        index = self._click_element(label, role)
        return {"clicked_element": index, "label": label, "role": role}

    @command
    def set_value(self, label: str, value: str, role: str = "") -> dict:
        """Set a unique enabled field by exact label and optional exact role.

        Requires native cua-driver set_value support. Raises on missing or
        ambiguous matches or rejected input; does not fall back to typing.
        """
        element = self._find_element(label, role)
        self._run(self._cua.set_value(int(element["element_index"]), value))
        return {"label": label, "role": role, "value": value}

    @command
    def type_text(self, text: str) -> dict:
        """Type text into the focused control; use click_element to select it first."""
        self._run(self._cua.type_text(text))
        return {"typed": text}

    @command
    def scroll(self, direction: str = "down", amount: int = 3) -> dict:
        """Scroll the foreground target application.

        direction must be up, down, left, or right. amount is a positive integer
        in the units used by the installed cua-driver.
        """
        if direction not in {"up", "down", "left", "right"}:
            raise ValueError("direction must be up, down, left, or right")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive integer")
        self._screenshot()
        self._run(self._cua.scroll(direction=direction, amount=amount))
        return {"direction": direction, "amount": amount}

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
    def click_at(self, x: int, y: int, capture_id: str) -> dict:
        """Click screenshot pixels using the capture_id returned by capture_screenshot.

        Requires the same visible window and geometry. The ID is single-use;
        later captures or input invalidate it. Does not move the window, capture
        another image, or retry input. Capture separately to inspect the result.
        """
        result = self._run(self._cua.click_at(x=x, y=y, capture_id=capture_id))
        return {"x": x, "y": y, "capture_id": capture_id,
                "effect": CuaDriverClient._structured(result).get("effect", "unverifiable")}
