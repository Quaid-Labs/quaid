"""Host platform identity types.

Lives in lib so adapters and lib modules can describe the host without
importing from core.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class HostInfo:
    """Information about the host platform."""
    platform: str           # "openclaw", "claude-code", "standalone"
    version: str            # "2026.3.7", "2.1.72", etc.
    binary_path: Optional[str] = None  # Path to the host binary (for mtime)

    def label(self) -> str:
        return f"{self.platform} {self.version}"
