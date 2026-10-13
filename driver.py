"""Example machine workflows and screenshot inspection for PUDA.

Only high-level workflows and capture_screenshot are remotely callable.
GuiDriver supplies inherited Python operations for internal composition.
"""

import math

from puda import command

from gui_driver import ElementNotFoundError, GuiDriver


class Driver(GuiDriver):
    """Example instrument driver exposing screenshot inspection and a verified file import."""

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

    # ── Screenshot inspection ──────────────────────────────────────────────
    # The only low-level remote command. Input helpers remain inherited Python operations.

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
