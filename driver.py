"""Machine-specific driver: add this instrument's PUDA commands here.

GuiDriver provides desktop automation, lifecycle management, cached state, and
generic GUI commands. Driver defines the instrument's operations using those
helpers, just as the direct Python edge template wraps a device SDK.

Decorate remotely callable methods with @command. Document parameters and return
types, use JSON-serializable values, and raise on failure. Use @safety for hazard
metadata and enforce actual preconditions in the command body.
"""

from gui_driver import GuiDriver


class Driver(GuiDriver):
    """TODO: Describe this instrument and the operations its GUI exposes."""

    # The inherited constructor accepts target_app from main.py. If you add
    # configuration or cached state, call super().__init__(target_app) first.
    #
    # TODO: Add machine-specific commands. For example, after adapting the
    # button label and completion check to the actual application:
    #
    # from puda import command  # place this import at module level
    #
    # @command
    # def start_run(self) -> dict:
    #     """Start the instrument run and return its resulting screen."""
    #     self.click_element("Start run")
    #     # Check the application's expected running state before returning.
    #     return {"image_b64": self.capture_screenshot()["image_b64"]}
    #
    # Available helpers:
    #   self.click_element(label, role)       click one exact accessibility match
    #   self._choose_file_in_dialog(path)     fill and confirm an open file dialog
    #   self.capture_screenshot()             return PNG and capture metadata
    #   self.press_key("enter")               dispatch a key press
