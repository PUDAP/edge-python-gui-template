# edge-python-template

Template for a machine edge service in Python (PUDA CLI **v0.1.2**, Python SDK **0.0.18**). Use this to scaffold a new machine integration.

## Repo Structure

```
edge-python-template/
├── pyproject.toml          # Python project dependencies
├── uv.lock                 # Locked dependency versions (uv)
├── main.py                 # Main edge service — NATS + driver instance
├── driver.py               # Machine driver: @command methods and @tlm_stream telemetry
├── Dockerfile              # Container build
├── compose.yml             # Docker Compose
├── start_edge.bat          # Windows launcher script
├── .env.example            # Environment variable template
├── .dockerignore           # Docker build context exclusions
└── .gitignore              # Git ignore rules
```

## AI Agent Instructions

Follow these steps in order to adapt this template for a new machine.

### 1. Choose a unique Machine ID in your env

Pick a short, lowercase, hyphen-separated identifier for the machine and edit the `.env` file. This becomes `MACHINE_ID` everywhere.

### 2. Complete all TODOs

Search the repo for every `TODO` marker and resolve each one — typically renaming placeholder strings, filling in package metadata, and updating import paths to match the chosen `MACHINE_ID`.

### 3. Implement the Driver

Add the machine driver source in `driver.py`. The driver must expose a class that `puda.EdgeRunner` can wrap.

- Require `puda>=0.0.18` in `pyproject.toml` (already set in this template). Add other hardware dependencies as needed.
- Put a one-sentence summary in the **class docstring**. `puda machine list` and `puda machine info` show that first paragraph as `description`.
- Decorate every remotely callable method with `@command`. Undecorated methods are not advertised or callable. A driver with no `@command` methods fails at startup.
- Parameters should be JSON primitives (`str`, `int`, `float`, `bool`, `dict`, `list`) rather than custom class instances.
- Keep helpers private (`_name`). Raise on hardware failure. Returning `False` is still a successful PUDA response (`{"result": false}`).
- Attach `@safety` to commands that can cause harm. It is published in the catalog; it does not block edge dispatch, so enforce hard preconditions in the method body by raising. See `move_to` in `driver.py` (`confirm=True` so the agent prompts the operator before the move). Omit `@safety` on read-only commands.
- Mark methods that return live readings with `@tlm_stream(interval=...)`, e.g. position, weight, or temperature. `EdgeRunner` calls each one every `interval` seconds in a separate worker thread and publishes the returned dict to `puda.<MACHINE_ID>.tlm.stream.<name>`. Streams can run at the same time as commands. Return `None` to skip a sample. Remove the example `get_position` stream if the machine has no position.
- Return machine-level state (homed flag, loaded labware, current mode) from the `@machine_state` method. Its dict is merged into every MACHINE_STATE update read by `puda machine state`. At most one method may be marked.
  - It is called on each state change (startup, command start and end, errors, shutdown), not on a timer, so values that change outside commands belong in a `@tlm_stream`.
  - It runs on the edge's event loop. Return values the driver already has in memory; a slow device read here stalls commands and telemetry.
  - Don't return `state`, `run_id`, or `timestamp`; PUDA sets those.

### 4. Add Driver-Specific Environment Variables

In `.env`, add any new config fields below `MACHINE_ID` (e.g. `${MACHINE_ID}_PORT`, `${MACHINE_ID}_HOST`). Keep `NATS_SERVERS` and `MACHINE_ID` as-is.

### 5. Wire the Driver into main.py

In `main.py`:

1. Add any driver-specific environment variables to `Config` (e.g. device port, IP address).
2. Instantiate the driver using those config fields.

`EdgeRunner` publishes the heartbeat, host health (CPU, memory, temperature), every `@tlm_stream`, and the `@machine_state` fields on its own, so `main.py` only needs config and wiring.

### 6. Update the Dockerfile

In `Dockerfile`, add any system-level dependencies your driver needs (e.g. `libusb-dev` for USB devices).

Replace this file with a machine-specific README describing what the machine does, how to connect to it, and any hardware prerequisites.

---

## Environment Setup

From repo root:

```bash
cp .env.example .env
```

Edit `.env` and fill in:

- `MACHINE_ID` — machine identifier
- `NATS_SERVERS` — comma-separated NATS server URLs
- Any additional driver-specific variables

## Run With Docker (Recommended)

All commands below run from repo root.

Build and start:

```bash
docker compose -f compose.yml up -d --build
```

View logs:

```bash
docker compose -f compose.yml logs -f
```

Stop:

```bash
docker compose -f compose.yml down
```

## Run Baremetal (uv)

```bash
uv sync
uv run python main.py
```

## Verify

With the edge running, check that it advertises its description and telemetry streams, then watch one:

```bash
puda machine info <MACHINE_ID>
puda machine watch 'puda.<MACHINE_ID>.tlm.stream.>'
```

## Build and Push Image (GHCR)

Login:

```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin
```

Build and push:

```bash
docker compose -f compose.yml build
docker compose -f compose.yml push
```

## Notes

- Docker build context is the repository root.
- Dockerfile path is `Dockerfile`.
- `MACHINE_ID` in `.env` is used for NATS subject routing and Docker image/container naming.
