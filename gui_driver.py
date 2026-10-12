"""Reusable PUDA GUI automation base built on the cua-driver MCP client.

Works on Windows and Linux (X11). No vendor SDK and no LLM: the driver talks to the
target application through `cua-driver` (MCP), using the accessibility tree where
possible and raw screenshots otherwise.

Machine-specific commands belong in driver.py, whose Driver subclasses GuiDriver.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from concurrent.futures import Future
from copy import deepcopy
from typing import Any

from cua_client import CuaDriverClient

logger = logging.getLogger(__name__)


class ElementNotFoundError(RuntimeError):
    """A fresh accessibility capture has no enabled match for the selector."""


class GuiDriver:
    """
    PUDA GUI driver — controls a desktop instrument application via cua-driver.

    Follows the PUDA 0.1.x edge contract:
      * This class exposes no remote commands. Subclasses in ``driver.py``
        choose remote operations using ``@command``.
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
        self._ready: Future[None] = Future()
        self._stop_requested = asyncio.Event()
        self._closed = False
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="guidriver-async-loop"
        )
        self._loop_thread.start()
        self._client_lifetime = asyncio.run_coroutine_threadsafe(self._serve_client(), self._loop)
        try:
            self._ready.result(timeout=60)
        except BaseException:
            self._close()
            raise
        logger.info("GuiDriver ready — target_app=%r", self.target_app)

    # ── lifecycle (not remotely callable) ──────────────────────────────────

    def _run_loop(self) -> None:
        """Own the background loop and release its pending tasks on exit."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _serve_client(self) -> None:
        """Open and close the MCP contexts in the same persistent async task."""
        try:
            await self._cua.start()
            self._ready.set_result(None)
            await self._stop_requested.wait()
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise
        finally:
            await self._cua.stop()

    def _run(self, coro) -> Any:
        """Submit an async coroutine to the background loop and block until done."""
        if self._closed:
            coro.close()
            raise RuntimeError("GuiDriver is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60)

    def _snapshot(self) -> dict:
        """Cached state merged into every MACHINE_STATE update (must not touch the screen)."""
        return {"target_app": self.target_app, "last_status": self._last_status}

    def _close(self) -> None:
        """Release MCP and the background loop; main.py calls this on exit."""
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(self._stop_requested.set)
            self._client_lifetime.result(timeout=60)
        except Exception as e:
            logger.warning("GUI client cleanup: %s", e)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join()

    # ── generic GUI helpers ────────────────────────────────────────────────

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
                "Use click_at with coordinates taken from a fresh screenshot."
            )

    # ── native file-dialog helpers ─────────────────────────────────────────

    def _wait_file_dialog(self, appear: bool = True, timeout: float = 15.0) -> dict | None:
        """Wait for the requested dialog state; raise if it is not reached in time."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
        while True:
            dialog = self._run(self._cua._find_file_dialog(self.target_app))
            if (dialog is not None) == appear:
                return dialog
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if appear:
                    raise RuntimeError(
                        "The file dialog did not open. Open it first (click the app's "
                        "Import/Browse/Open button), then call _choose_file_in_dialog."
                    )
                raise RuntimeError("The file dialog did not close after choosing the file")
            time.sleep(min(0.5, remaining))

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
        self.capture_screenshot(include_elements=True)  # select the modal and inspect its fields
        elements = self._cua._last_elements
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
        if not (filename_fields and open_buttons and "set_value" in self._cua._tools):
            self._run(self._cua.type_text(file_path, delivery_mode="foreground"))
            self._run(self._cua.key("return", delivery_mode="foreground"))
            return

        self._run(self._cua.set_value(int(filename_fields[0]["element_index"]), file_path))
        open_button = max(
            open_buttons,
            key=lambda e: e.get("frame", {}).get("w", 0) * e.get("frame", {}).get("h", 0),
        )
        self._run(self._cua.click(element=int(open_button["element_index"])))

    def _choose_file_in_dialog(self, file_path: str, timeout: float = 15.0) -> None:
        """Fill the native file dialog with ``file_path`` and confirm it.

        Call this *after* your machine-specific code has clicked whatever opens the
        dialog (Import / Browse / Open...). Works with the Windows common dialog and
        the GNOME file chooser. Raises RuntimeError if the dialog never opens or
        does not close afterwards.
        """
        dialog = self._wait_file_dialog(timeout=timeout)
        assert dialog is not None  # appear=True returns a dialog or raises
        if "portal" in str(dialog.get("app_name", "")).casefold():
            self._type_into_portal_dialog(dialog, file_path)
        else:
            self._type_into_windows_dialog(file_path)

        self._wait_file_dialog(appear=False, timeout=timeout)

    # ── GUI operations (remote exposure belongs in driver.py) ─────────────

    def capture_screenshot(self, include_elements: bool = False) -> dict:
        """Capture a PNG and a copied context for coordinate clicks.

        Capturing foregrounds the app and invalidates earlier capture IDs.

        Args:
            include_elements: Refresh accessibility elements as well. Defaults to False.

        Returns:
            dict: image_b64 and capture. capture contains capture_id, pid, window_id,
            bounds, width, and height, or is None when coordinate input is unavailable.

        Raises:
            TypeError: include_elements is not a bool.
            RuntimeError: Capture fails, returns no image, or cannot provide requested elements.
        """
        result = self._run(self._cua.capture(
            app=self.target_app, include_elements=include_elements,
        ))
        img = CuaDriverClient.extract_image_b64(result)
        if img is None:
            raise RuntimeError(
                f"cua-driver returned no image for app={self.target_app!r}. "
                "Ensure the app is visible and screen capture is permitted."
            )
        self._last_status = {"captured_at": time.time()}
        return {"image_b64": img, "capture": deepcopy(self._cua._coordinate_capture)}

    def get_elements(self) -> dict:
        """Capture the application's current accessibility elements.

        Capturing invalidates earlier coordinate capture IDs.

        Returns:
            dict: elements, a list of accessibility records with labels, roles,
            indices, and any additional fields supplied by the backend.

        Raises:
            RuntimeError: The backend cannot capture the image or accessibility elements.
        """
        self.capture_screenshot(include_elements=True)
        return {"elements": self._cua._last_elements}

    def find_element(self, label: str, role: str = "") -> dict:
        """Find one enabled element by exact, case-sensitive label and optional role.

        Element indices belong to this fresh capture only.

        Args:
            label: Non-empty exact accessibility label.
            role: Exact accessibility role, or "" to match any role. Defaults to "".

        Returns:
            dict: A copy of the uniquely matching accessibility element.

        Raises:
            ValueError: label is empty.
            ElementNotFoundError: No enabled element matches.
            RuntimeError: Multiple elements match or capture fails.
        """
        if not label:
            raise ValueError("An exact non-empty element label is required")
        elements = self.get_elements()["elements"]
        matches = [
            element for element in elements
            if element.get("label") == label
            and (not role or element.get("role") == role)
            and element.get("enabled", True)
        ]
        if not matches:
            raise ElementNotFoundError(f"No enabled element matches label={label!r}, role={role!r}")
        if len(matches) > 1:
            raise RuntimeError(
                f"Ambiguous element: {len(matches)} matches for label={label!r}, role={role!r}. "
                "Inspect get_elements and specify a unique label and role, or use click_at."
            )
        return dict(matches[0])

    def wait_for_element(
        self, label: str, role: str = "", timeout: float = 30.0,
        poll_interval: float = 0.5, error_labels: list[str] | None = None,
    ) -> dict:
        """Poll fresh captures for one enabled exact label/role match.

        Known error labels take precedence over success in the same capture.
        Polling foregrounds the app and invalidates coordinate capture IDs. Input
        is never retried. An in-flight capture has its own timeout.

        Args:
            label: Non-empty exact, case-sensitive accessibility label.
            role: Exact accessibility role, or "" for any role. Defaults to "".
            timeout: Finite non-negative polling timeout in seconds. Defaults to 30.
            poll_interval: Finite positive delay between captures in seconds.
                Defaults to 0.5.
            error_labels: Exact labels indicating failure, including disabled text.
                Defaults to None, which disables known-error detection.

        Returns:
            dict: The uniquely matching enabled element observed before returning.

        Raises:
            ValueError: label is empty or timing arguments are invalid.
            TimeoutError: The expected element remains absent at the deadline.
            RuntimeError: A known error appears, matches are ambiguous, or capture fails.
        """
        if not label:
            raise ValueError("An exact non-empty element label is required")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be finite and positive")
        deadline = time.monotonic() + timeout
        while True:
            try:
                element = self.find_element(label, role)
            except ElementNotFoundError:
                element = None
            errors = [e.get("label") for e in self._cua._last_elements
                      if e.get("label") in (error_labels or [])]
            if errors:
                raise RuntimeError(f"Application reported an error while waiting for {label!r}: {errors}")
            if element is not None:
                return element
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for label={label!r}, role={role!r}")
            time.sleep(min(poll_interval, remaining))

    def click_element(self, label: str, role: str = "") -> dict:
        """Click one enabled element by exact, case-sensitive label and optional role.

        Resolve the element from a fresh capture and send one click. Successful
        delivery alone does not confirm completion of the application's operation.

        Args:
            label: Non-empty exact accessibility label.
            role: Exact accessibility role, or "" for any role. Defaults to "".

        Returns:
            dict: clicked_element index, label, and role.

        Raises:
            ValueError: label is empty.
            ElementNotFoundError: No enabled element matches.
            RuntimeError: Matches are ambiguous, coordinates are outside the window,
                capture fails, or input is rejected.
        """
        selected = self.find_element(label, role)
        self._validate_element_position(selected, label)
        index = int(selected["element_index"])
        self._run(self._cua.click(element=index))
        return {"clicked_element": index, "label": label, "role": role}

    def set_value(self, label: str, value: str, role: str = "") -> dict:
        """Set a uniquely matched field through native accessibility.

        Requires native set_value support and does not fall back to typing.
        Verify the application-specific result after setting the field.

        Args:
            label: Non-empty exact, case-sensitive accessibility label.
            value: Text value to assign to the field.
            role: Exact accessibility role, or "" for any role. Defaults to "".

        Returns:
            dict: label, role, and the submitted value.

        Raises:
            ValueError: label is empty.
            ElementNotFoundError: No enabled element matches.
            RuntimeError: Matches are ambiguous, capture fails, native setting is
                unavailable, or input is rejected.
        """
        element = self.find_element(label, role)
        self._run(self._cua.set_value(int(element["element_index"]), value))
        return {"label": label, "role": role, "value": value}

    def type_text(self, text: str) -> dict:
        """Type text into the focused control.

        Select the intended control with click_element before typing.

        Args:
            text: Text to send to the currently focused control.

        Returns:
            dict: typed, containing the submitted text.

        Raises:
            RuntimeError: The backend cannot dispatch typing or rejects the input.
        """
        self._run(self._cua.type_text(text))
        return {"typed": text}

    def scroll(self, direction: str = "down", amount: int = 3) -> dict:
        """Scroll the foreground target application.

        Capture the target before scrolling. This invalidates earlier capture IDs.

        Args:
            direction: One of "up", "down", "left", or "right". Defaults to "down".
            amount: Positive integer in the installed backend's scroll units.
                Defaults to 3.

        Returns:
            dict: direction and amount submitted to the backend.

        Raises:
            ValueError: direction or amount is invalid.
            RuntimeError: Capture fails, scrolling is unavailable, or input is rejected.
        """
        if direction not in {"up", "down", "left", "right"}:
            raise ValueError("direction must be up, down, left, or right")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive integer")
        self.capture_screenshot()
        self._run(self._cua.scroll(direction=direction, amount=amount))
        return {"direction": direction, "amount": amount}

    def press_key(self, keys: str) -> dict:
        """Press a key or shortcut in the target application.

        Args:
            keys: Key combination, for example "escape", "ctrl+s", or "f5".

        Returns:
            dict: keys, containing the submitted key combination.

        Raises:
            RuntimeError: The backend cannot dispatch the key or rejects the input.
        """
        self._run(self._cua.key(keys))
        return {"keys": keys}

    def click_at(self, x: int, y: int, capture_id: str) -> dict:
        """Click pixels from a specific screenshot without recapturing or retrying.

        Requires the same visible window and geometry. The capture ID is single-use
        and invalidated by later captures or input. Capture separately for feedback.

        Args:
            x: Integer horizontal screenshot pixel coordinate, 0 <= x < capture width.
            y: Integer vertical screenshot pixel coordinate, 0 <= y < capture height.
            capture_id: ID from capture_screenshot()["capture"]["capture_id"].

        Returns:
            dict: x, y, capture_id, and the reported input effect (or "unverifiable").

        Raises:
            ValueError: Coordinates are not integers inside the referenced screenshot.
            RuntimeError: The ID is stale or unknown, the window changed or disappeared,
                or foregrounding or clicking is rejected.
        """
        result = self._run(self._cua.click_at(x=x, y=y, capture_id=capture_id))
        return {"x": x, "y": y, "capture_id": capture_id,
                "effect": CuaDriverClient._structured(result).get("effect", "unverifiable")}
