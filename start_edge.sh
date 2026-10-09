#!/usr/bin/env bash
# Linux launcher for the GUI edge service (Windows: start_edge.bat).
#
# The target application and this driver must run in the same X11 desktop session.
# Native Wayland windows are invisible to cua-driver (log in with "Ubuntu on Xorg").
cd "$(dirname "$0")" || exit 1

if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
  echo "WARNING: this is a Wayland session; cua-driver cannot see Wayland windows." >&2
  echo "         Log in with an X11 session (e.g. 'Ubuntu on Xorg')." >&2
fi
if [ -z "${DISPLAY:-}" ]; then
  echo "WARNING: DISPLAY is not set; run this from a terminal inside the desktop session," >&2
  echo "         or export DISPLAY and XAUTHORITY first." >&2
fi

# Cursor/VS Code terminals set this; it makes Electron apps start as plain Node and exit.
unset ELECTRON_RUN_AS_NODE

exec uv run python main.py
