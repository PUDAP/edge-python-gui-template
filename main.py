"""
Main entry point for the GUI driver edge service.

Integrates any desktop instrument software into PUDA using Hermes computer use
(cua-driver MCP) — no instrument SDK and no LLM required. Works on Windows and
Linux (X11).

Configuration is loaded from a .env file (see .env.example).
EdgeRunner publishes heartbeats and host health automatically; the driver's
``_snapshot`` supplies the machine state.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# PUDA discovers host addresses by reading command output in text mode. Windows
# otherwise uses the active ANSI code page (commonly cp1252), which cannot decode
# all UTF-8 output from tools such as `tailscale status --json`. UTF-8 mode is an
# interpreter-startup setting, so restart once before importing PUDA.
if os.name == "nt" and not sys.flags.utf8_mode:
    os.environ["PYTHONUTF8"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])

from puda import EdgeNatsClient, EdgeRunner
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from driver import Driver

# ── logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ── configuration ──────────────────────────────────────────────────────────

class Config(BaseSettings):
    """
    Environment-driven configuration for the GUI driver edge service.

    All fields are read from .env (or real environment variables).
    Field names are case-insensitive.
    """

    # ── PUDA core ──────────────────────────────────────────────────────────
    machine_id: str = Field(
        description="Unique PUDA machine identifier (e.g. 'hplc-1', 'ot2-1')."
    )
    nats_servers: str = Field(
        description="Comma-separated NATS server URLs, e.g. nats://bears:4222."
    )

    # ── GUI driver ─────────────────────────────────────────────────────────
    target_app: str = Field(
        description=(
            "Window title (or app name) that cua-driver should target, "
            "e.g. 'Chromeleon', 'Opentrons', 'Notepad'."
        )
    )

    puda_nats_ca: str = ""
    puda_e2e_key_file: str = ""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def nats_server_list(self) -> list[str]:
        return [s.strip() for s in self.nats_servers.split(",") if s.strip()]


def load_config() -> Config:
    """Load and validate config; exit the process on failure."""
    try:
        return Config()
    except Exception as e:
        logger.error("Failed to load configuration: %s", e, exc_info=True)
        sys.exit(1)


# ── main ───────────────────────────────────────────────────────────────────

async def main() -> None:
    config = load_config()

    logger.info("=== GUI Driver Configuration ===")
    logger.info("  machine_id   : %s", config.machine_id)
    logger.info("  nats_servers : %s", config.nats_servers)
    logger.info("  target_app   : %s", config.target_app)
    logger.info("================================")

    logger.info("Initialising Driver for %r…", config.target_app)
    driver = Driver(target_app=config.target_app)

    try:
        logger.info("Connecting to NATS at %s…", config.nats_servers)
        edge_nats_client = EdgeNatsClient(
            servers=config.nats_server_list,
            machine_id=config.machine_id,
            ca_file=config.puda_nats_ca,
            e2e_key_file=config.puda_e2e_key_file,
        )

        runner = EdgeRunner(
            nats_client=edge_nats_client,
            machine_driver=driver,
            state_handler=driver._snapshot,
        )
        await runner.connect()

        logger.info(
            "==================== %s GUI Edge Service Ready ====================",
            config.machine_id,
        )
        await runner.run()
    finally:
        driver._close()


# ── retry loop ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.warning("Gracefully stopping…")
            sys.exit(0)
        except Exception as e:
            logger.error("Fatal error: %s", e, exc_info=True)
            logger.info("Retrying in 5 s…")
            time.sleep(5)
