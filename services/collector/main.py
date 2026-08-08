"""Executable entry point for the collector service foundation."""

from services.collector import SERVICE_NAME
from services.collector.config import load_settings
from services.collector.logging_config import get_logger


def main() -> None:
    """Validate configuration and identify the service without starting work."""
    load_settings()
    logger = get_logger("services.collector.main")
    logger.info(
        "%s is initialized.",
        SERVICE_NAME,
        extra={"event": "service_initialized"},
    )


if __name__ == "__main__":
    main()
