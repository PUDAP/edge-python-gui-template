"""Example machine driver: adapt commands and labels to your instrument.

GuiDriver supplies ordinary Python GUI operations. Only methods decorated here
with @command are remotely callable. This template explicitly exposes the public
GUI operations for agent-driven exploration alongside a high-level example.
Remove decorated overrides that a deployed machine does not need to expose.

Low-level commands delegate to GuiDriver for inspection and individual actions.
High-level commands compose those operations with machine-specific preconditions
and completion checks. Add instrument workflows in the high-level section.
"""

import math

from puda import command

from gui_driver import ElementNotFoundError, GuiDriver


class Driver(GuiDriver):
    """Example instrument driver exposing GUI operations and a verified file import."""

    # TODO: Replace the description and import workflow with your instrument's
    # operations. Add machine-specific configuration and cached state here.

    def __init__(self, target_app: str) -> None:
        """Connect the GUI backend for this instrument.

        Args:
            target_app: Application name or window-title substring to target.

        Raises:
            RuntimeError: The GUI backend cannot start or initialize.
            TimeoutError: Backend initialization exceeds its timeout.
        """
        super().__init__(target_app=target_app)

    # ── High-level machine commands ────────────────────────────────────────
    # Add instrument workflows here. Validate preconditions, perform actions
    # once, and verify an operation-specific result before returning success.

    @command
    def import_file(
        self, file_path: str, success_label: str, success_role: str = "", timeout: float = 30.0,
    ) -> dict:
        """Example: import a file and wait for a new, explicit success indicator.

        Adapt Import/Browse, the error label, and the completion selector to the
        real application. A generic Ready label is insufficient: use an indicator
        that confirms this particular file loaded, not just that it was selected.

        Args:
            file_path: Non-empty path accessible on the instrument's desktop host.
            success_label: Exact post-import label, absent before the operation.
            success_role: Optional exact role of that success indicator. Defaults to "".
            timeout: Finite non-negative timeout in seconds for each wait phase.
                Defaults to 30.

        Returns:
            dict: imported_file, returned only after the success indicator is observed.

        Raises:
            ValueError: A required path or label is empty, or timeout is invalid.
            TimeoutError: A required UI element does not appear before its deadline.
            RuntimeError: Success was already present, a known error appears,
                a dialog times out, or a GUI operation fails.
        """
        if not file_path.strip():
            raise ValueError("file_path must not be empty")
        if not success_label.strip():
            raise ValueError("success_label must not be empty")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")
        # Reject stale success from a previous operation before sending input.
        try:
            self.find_element(success_label, success_role)
        except ElementNotFoundError:
            pass
        else:
            raise RuntimeError("Success indicator already exists; choose a fresh operation-specific check")
        # TODO: Replace these exact labels and error indicators for your app.
        self.click_element("Import")
        self.wait_for_element("Browse", timeout=timeout, error_labels=["Import failed"])
        self.click_element("Browse")
        self._choose_file_in_dialog(file_path, timeout=timeout)
        self.wait_for_element(
            success_label, success_role, timeout=timeout, error_labels=["Import failed"],
        )
        return {"imported_file": file_path}

    # ── Low-level GUI commands ─────────────────────────────────────────────
    # Explicit remote access for exploration and debugging. Implement reusable
    # GUI behavior in GuiDriver; keep these overrides as thin delegates.

    @command
    def capture_screenshot(self, include_elements: bool = False) -> dict:
        """Capture a PNG and a copied context for coordinate clicks.

        Capturing foregrounds the app and invalidates earlier capture IDs.

        Args:
            include_elements: Include accessibility elements from the same capture.
                Defaults to False.

        Returns:
            dict: image_b64 and capture. capture contains capture_id, pid, window_id,
            bounds, width, and height, or is None when coordinate input is unavailable.
            When include_elements=True, also includes a copied elements list.

        Raises:
            TypeError: include_elements is not a bool.
            RuntimeError: Capture fails, returns no image, or cannot provide requested elements.
        """
        return super().capture_screenshot(include_elements=include_elements)

    @command
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
        return super().find_element(label=label, role=role)

    @command
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
        return super().wait_for_element(
            label=label, role=role, timeout=timeout,
            poll_interval=poll_interval, error_labels=error_labels,
        )

    @command
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
        return super().click_element(label=label, role=role)

    @command
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
        return super().set_value(label=label, value=value, role=role)

    @command
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
        return super().type_text(text=text)

    @command
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
        return super().scroll(direction=direction, amount=amount)

    @command
    def press_key(self, keys: str) -> dict:
        """Press a key or shortcut in the target application.

        Args:
            keys: Key combination, for example "escape", "ctrl+s", or "f5".

        Returns:
            dict: keys, containing the submitted key combination.

        Raises:
            RuntimeError: The backend cannot dispatch the key or rejects the input.
        """
        return super().press_key(keys=keys)

    @command
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
        return super().click_at(x=x, y=y, capture_id=capture_id)
