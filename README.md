# edge-gui-python-template

A template for PUDA edge services that integrate **any desktop instrument software** into PUDA without a hardware SDK. Copy it once per machine, set the window name, and add that machine's commands. It runs on **Windows** and **Linux (X11)**.

| Layer | Technology | Role |
|---|---|---|
| GUI automation | [Hermes computer use](https://hermes-agent.nousresearch.com/docs/user-guide/features/computer-use) → `cua-driver` MCP | Capture the app and dispatch accessibility / keyboard / mouse actions |
| PUDA integration | `puda==0.0.18` `EdgeRunner` + NATS | Expose allowlisted commands and machine state to the PUDA platform |

There is **no LLM**: screens are returned as raw base64 PNG, and the driver selects elements by exact, case-sensitive accessibility labels and optional roles.

---

## Repo Structure

```
edge-gui-python-template/
├── pyproject.toml    # dependencies (puda, mcp, pydantic-settings)
├── main.py           # Config → Driver → EdgeNatsClient → EdgeRunner
├── driver.py         # Driver: machine-specific commands built on GuiDriver
├── gui_driver.py     # GuiDriver: undecorated GUI operations and lifecycle
├── cua_client.py     # CuaDriverClient: MCP connection, window discovery and input
├── tests/            # offline tests for the generic logic
├── .env.example      # environment variable template
├── start_edge.bat    # Windows launcher
├── start_edge.sh     # Linux launcher
├── Dockerfile        # container build (optional)
└── compose.yml       # docker compose (optional)
```

---

## How It Works

```
PUDA CLI / Protocol
       │  NATS
       ▼
  EdgeRunner ───────────►  Driver (@command methods)
   (state_handler =                │
    driver._snapshot)          GuiDriver
                                   │
                              CuaDriverClient
                                   │
                             cua-driver MCP
                                   │
                          ┌────────▼─────────┐
                          │ Instrument       │
                          │ software (any)   │
                          └──────────────────┘
```

This follows the PUDA 0.1.x edge contract:

1. Only methods marked `@command` are remotely callable.
2. Internal helpers, startup, cached state and telemetry use a leading underscore.
3. `EdgeRunner` publishes heartbeat and host-health telemetry; `main.py` passes `driver._snapshot` as the state handler.
4. Command signatures and docstrings are published in the command catalog, so every command needs type hints and a docstring.

---

## Prerequisites

### 1. Install Hermes Agent with computer use (both platforms)

```bash
hermes computer-use install
hermes computer-use doctor      # all checks should be green
```

`cua-driver` must be on your `PATH`.

### 2. Python environment (uv)

```bash
uv sync
```

### 3. Platform requirements

**Windows**
- Run from an interactive session (RDP or console). Over SSH (Session 0) the GUI is not reachable; use the [Windows SSH guide](https://cua.ai/docs/how-to-guides/driver/windows-ssh) or the cua-driver autostart task.
- `main.py` restarts itself once in UTF-8 mode so PUDA can decode command output.

**Linux**
- **Use an X11 session.** On GNOME/Wayland, native Wayland windows (including the file chooser) are invisible to cua-driver. At the login screen click your user, then the ⚙ icon at the bottom right and pick **Ubuntu on Xorg**. Check with `echo $XDG_SESSION_TYPE` → `x11`.
- Start the driver from a terminal **inside** that desktop session (or export `DISPLAY` and `XAUTHORITY`). The driver forwards `DISPLAY`, `XAUTHORITY`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, `XDG_SESSION_TYPE` and `DBUS_SESSION_BUS_ADDRESS` to cua-driver.
- Electron apps may need `--no-sandbox --force-renderer-accessibility` to start and to expose their accessibility tree.
- Terminals inside Cursor / VS Code set `ELECTRON_RUN_AS_NODE=1`, which makes Electron apps exit silently. `unset ELECTRON_RUN_AS_NODE` before launching one (`start_edge.sh` does this for the driver).
- The target window needs desktop focus for input; the driver brings it to the foreground before capturing.

---

## Setup

```bash
cp .env.example .env        # Windows: Copy-Item .env.example .env
```

| Variable | Description |
|---|---|
| `MACHINE_ID` | Unique PUDA machine ID, e.g. `hplc-1` |
| `NATS_SERVERS` | NATS cluster URLs |
| `TARGET_APP` | Window title (or a unique part of it) of the instrument software |

### Finding the TARGET_APP name

`TARGET_APP` is matched case-insensitively against the window **title** and the **app name** as cua-driver sees them.

```powershell
# Windows
Get-Process | Where-Object {$_.MainWindowTitle} | Select-Object MainWindowTitle
```

```bash
# Linux (X11)
xwininfo -root -tree | grep '"'
```

---

## Run

```bash
# Windows
.\start_edge.bat
# Linux
./start_edge.sh
# Or directly
uv run python main.py
# Or with Docker
docker compose -f compose.yml up -d --build
```

---

## GUI operations and remote commands

`driver.py` starts with an explicit constructor calling `super().__init__(target_app=target_app)`, followed by high-level machine commands and then low-level GUI commands. The high-level section contains workflows such as `import_file`, including preconditions and completion checks. The low-level section contains thin delegates for inspection and individual actions. Add new instrument workflows to the high-level section.

`GuiDriver` has no `@command` decorators. Its methods below are ordinary Python operations available to machine code. The example `Driver` explicitly exposes all of them through decorated overrides, alongside `import_file`. This lets an agent inspect the live GUI, test individual steps, and build high-level machine commands with verified completion. Adapt the import workflow's labels and completion checks before using it. Remove any decorated overrides that a deployed machine does not need; the inherited Python operations remain available internally.

| Python operation | Description |
|---|---|
| `capture_screenshot` | Return `image_b64` and a copied `capture` context (or null) |
| `get_elements` | Inspect current accessibility elements, labels, and roles |
| `find_element` | Find one enabled exact label/role match; reject ambiguity |
| `wait_for_element` | Poll for an exact enabled match; raise on timeout or known error indicators |
| `click_element` | Resolve and click one exact label/role match from a fresh capture |
| `set_value` | Set a uniquely matched field through native accessibility |
| `scroll` | Scroll up, down, left, or right by a positive amount |
| `type_text` | Type text into the focused control |
| `press_key` | Send a keyboard shortcut (e.g. `ctrl+s`, `f5`) |
| `click_at` | Click screenshot pixels using its `capture_id`; reject changed window geometry |

Constructing `Driver` connects the MCP client. `main.py` calls internal `_close()` in a `finally` block on normal exit, cancellation, or failure before retrying. Use `press_key("escape")` to send Escape; there is no generic reset command.

### Example CLI usage

```bash
puda machine run my-instrument capture_screenshot
puda machine run my-instrument get_elements
puda machine run my-instrument find_element --label "Import"
puda machine run my-instrument click_element --label "Import"
puda machine run my-instrument wait_for_element --label "Browse" --timeout 15
# After adapting the example workflow to your instrument:
puda machine run my-instrument import_file --file_path "/path/on/desktop/method.xml" --success_label "Loaded method.xml"
```

---

### Command migration

Machine commands call the public methods directly: `find_element`, `click_element`, and `capture_screenshot(include_elements=False)`. The redundant private wrappers have been removed. Use `capture_screenshot()["image_b64"]` when only the PNG is needed.

Command responses omit the configured app name. `capture_screenshot` returns `image_b64` and `capture`; `get_elements` returns `elements`. The configured `target_app` remains in machine state for diagnostics. The copied `capture` context contains `capture_id`, `pid`, `window_id`, `bounds` (window x/y/width/height), and screenshot pixel `width`/`height`.

`click_at(x, y, capture_id)` requires `result["capture"]["capture_id"]` from the screenshot used to choose coordinates. The ID is single-use and invalidated by subsequent captures or input. The client verifies the exact window's identity, visibility, and bounds before and after foregrounding it; it does not reposition the window or recapture. Call `capture_screenshot` separately for post-click feedback. If capture metadata or native targeted input is unavailable, `capture` is null and coordinate clicks are unavailable. These checks cannot detect changed content inside an otherwise unchanged window or prevent an external change between validation and dispatch.

`capture_screenshot` replaces both `status` and `ask_screen`; interpret images in the calling agent. Replace `find_and_click(description)` with `click_element(label, role)` using exact, case-sensitive values from `get_elements`. Replace `type_text(text, field_description)` with separate `click_element` and `type_text` calls. Machine helpers now use `click_element(label, role)` instead of the former fuzzy helper.

Matching considers enabled elements only. Missing or duplicate matches raise before input. `get_elements` requires a capture backend that supplies accessibility elements; screenshot-only backends cannot support element lookup. This does not change the client's existing window-selection heuristics or guarantee that the UI action completed; machine commands should check their expected result.

## Deploying a New Machine

1. **Copy the template** and install: `git clone … my-machine-guidriver && cd my-machine-guidriver && uv sync`.
2. **Set `.env`**: `MACHINE_ID`, `NATS_SERVERS`, `TARGET_APP`.
3. **Add the machine's commands** to `Driver` in `driver.py` and decorate them with `@command`. It subclasses `GuiDriver` and explicitly publishes its public operations through decorated overrides. The private file-dialog helper remains internal. Build machine commands from these operations:

   | Helper | Use |
   |---|---|
   | `self.click_element("Start run")` | click one exact accessibility label/role match; reject ambiguity |
   | `self._run(self._cua.key("ctrl+s"))` / `type_text(...)` / `click(...)` | raw input |
   | `self._choose_file_in_dialog(path)` | fill a native file dialog (Windows *Open* or GNOME *Open Files*) after your code has clicked Import/Browse |
   | `self.capture_screenshot()["image_b64"]` | raw base64 PNG of the window |
   | `self.capture_screenshot(include_elements=True)` | PNG plus a refreshed accessibility element cache |

   See `Driver.import_file` for an executable example that checks for stale success, waits for Browse, submits the file, and waits for the operation's completion indicator. `success_label` is explicit in this template; a production driver should derive it from the requested file or encode an application-specific completion check.

4. **Rename the driver class if you like** (e.g. `HplcDriver`) and update the import in `main.py`. Keep machine-specific commands in `driver.py`; `gui_driver.py` provides reusable automation helpers and `cua_client.py` handles the MCP transport and desktop input. If you override the constructor, call `super().__init__(target_app)`.
5. **Run the tests** (`uv run python -m unittest discover -s tests`) and try each command against the real app.

### Building commands that confirm completion

Use Google-style docstrings for every public operation and command: a one-sentence summary, relevant behavior or preconditions, `Args:` describing every parameter (including units, limits, and defaults), `Returns:` naming the returned fields, and `Raises:` for expected failures. Omit `Args:` for parameterless methods. Keep command docstrings self-contained because PUDA publishes them in the command catalog. See `Driver.import_file` for the complete format; do not put YAML fences inside Python docstrings.

PUDA reports success when a command returns. Each command must define what that return proves. A successful click, closed file dialog, or elapsed sleep does not prove the instrument completed an operation.

`import_file` calls `wait_for_element` for verification and returns only `{"imported_file": file_path}` after that check succeeds. Failed verification raises instead of returning a false result. The command does not take an extra screenshot after verification; request `capture_screenshot` separately when visual feedback is needed.

1. Check preconditions before input: the correct application state, required controls, and machine-specific constraints.
2. Choose a success indicator tied to this operation, such as the expected method name together with a loaded status. Avoid generic Ready labels or filenames that appear while a file is merely selected. Inspect actual accessibility labels and roles; the template's `Loaded method.xml` and `Import failed` are examples to replace.
3. Rule out stale success. The import example rejects a success label already present before input. Applications that retain status must instead use a job ID, state transition, or another operation-specific check.
4. Perform each action once. Wait for controls to become available between steps. `wait_for_element(label, role="", timeout=30, poll_interval=0.5, error_labels=[...])` captures fresh accessibility data and returns only one enabled exact match. It raises `TimeoutError` when absent; ambiguity and capture failures propagate immediately. Known error labels in the same capture take precedence over success, including disabled error text.
5. Return evidence after verification, such as the loaded method, observed status, or run ID. Raise on failure or timeout; returning `False` is still a successful PUDA response. Do not automatically repeat imports or start actions after a timeout: inspect the current state first.

The import example checks for a new exact success label after the dialog closes. It can only prove what that indicator means in your application. If completion requires multiple conditions (for example, loading has stopped and the correct method is active), implement that check in `driver.py`. If the application exposes no reliable accessibility status, use an application API, output artifact, or an explicit external verification step instead of reporting unverified success.

Define long-running operations precisely: `start_run` can return after observing Running, while `wait_for_run_completion` waits for the experiment to finish. Timeouts are per wait phase, not a total command deadline; an in-flight capture has its own timeout. Polling foregrounds the application and invalidates coordinate capture IDs.

Test delayed success, stale indicators, disabled controls, ambiguous matches, known errors, capture failures, and timeouts with mocked observations. Then validate against the real instrument application; offline tests cannot establish that a label actually means success.

### Telemetry

The base driver does not schedule screenshot telemetry. A machine that needs it can define its own method in `driver.py`, decorate it with `@tlm_stream(interval=30.0, name="instrument_status")` (imported from `puda`), and return `{"image_b64": self.capture_screenshot()["image_b64"]}`. Each capture brings the window to the foreground and invalidates any outstanding coordinate-click capture ID.

---

## Things to Know

- **The app repaints slowly.** Wait for an expected UI state with a bounded timeout rather than assuming a fixed sleep proves completion. `_wait_file_dialog` polls for dialog appearance and disappearance.
- **Prefer `click_element` over pixels.** Use `click_at` only with coordinates taken from a fresh screenshot.
- **Windows and Linux differ only at the edges:** the file-chooser (handled by `_choose_file_in_dialog`) and how the window is found. Page-level checks that rely on an app exposing its URL do not work on Linux accessibility trees; verify the screen instead.

---

## Troubleshooting

**`cua-driver not found`** — run `hermes computer-use install` and confirm `cua-driver --version` works in a new terminal.

**`No window matching 'X'`** — the app is not running, is minimised on another desktop, or (Linux) is a native Wayland window. Switch to an X11 session and check `TARGET_APP` against the window list above.

**No image returned from capture** — the window may be off-screen; the driver tries to restore it. On Windows run `hermes computer-use doctor`.

**`foreground_unavailable` / `background_unavailable`** — the window could not get input focus. Click the target window once, or make sure you are in an X11 session.

**`No accessible UI element matched …`** — use words that appear in the element's label, or take a screenshot and use `click_at`.

**File dialog is not found (Linux)** — it must be an X11 window (X11 session). The GNOME chooser is owned by `xdg-desktop-portal-gnome` and titled "Open Files".

**Electron app exits immediately (Linux)** — `unset ELECTRON_RUN_AS_NODE`; add `--no-sandbox` if you see a `chrome-sandbox` error.
