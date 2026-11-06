"""Re-export of the shared settings (bot and worker read the same `.env`)."""

from core.config import Settings, settings

__all__ = ["Settings", "settings"]
