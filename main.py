"""
Main entry point for the ${MACHINE_ID} machine edge service.

This module provides the main event loop for the machine, handling command
execution via NATS messaging, telemetry publishing, and connection management.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from puda import EdgeNatsClient, EdgeRunner
from driver import Driver


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logging.getLogger("driver").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# Environment configuration
class Config(BaseSettings):
    machine_id: str
    nats_servers: str
    # TODO: Replace with your driver's config fields (e.g. IP address, baud rate)
    device_port: str

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def nats_server_list(self) -> list[str]:
        return [s.strip() for s in self.nats_servers.split(",") if s.strip()]


def load_config() -> Config:
    """Load and validate configuration; exit process on failure."""
    try:
        return Config()
    except Exception as e:
        logger.error("Failed to load configuration: %s", e, exc_info=True)
        sys.exit(1)


async def main():
    """Initialize the machine driver and NATS client, then run the edge runner."""
    config = load_config()
    logger.info("Config loaded for %s", config.machine_id)
    logger.info("Full config: %s", config.model_dump())

    logger.info("Initializing machine driver")
    driver = Driver(port=config.device_port)
    logger.info("Machine driver initialized successfully")

    logger.info("Connecting to NATS at %s", config.nats_servers)
    edge_nats_client = EdgeNatsClient(
        servers=config.nats_server_list,
        machine_id=config.machine_id,
    )

    runner = EdgeRunner(nats_client=edge_nats_client, machine_driver=driver)
    await runner.connect()
    logger.info("NATS client initialized successfully")
    logger.info(
        "==================== %s Edge Service Ready. Publishing telemetry... ====================",
        config.machine_id,
    )
    await runner.run()


# Run main in a loop; retry on fatal errors, exit gracefully on KeyboardInterrupt.
if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.warning("Gracefully stopping...")
            sys.exit(0)
        except Exception as e:
            logger.error("Fatal error: %s", e, exc_info=True)
            time.sleep(5)
