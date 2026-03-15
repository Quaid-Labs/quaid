"""Platform compatibility guard.

Quaid requires macOS or Linux. It uses Unix-only system APIs:
  - fcntl (file locking) — used in 10+ files
  - os.fork / os.setsid (daemon process spawning)
  - socket.AF_UNIX (platform scheduler IPC)

Windows support would require abstracting all three. See docs/WINDOWS-COMPAT.md
for the full compat roadmap if this becomes a priority.

This module is imported by lib/adapter.py so the check fires on any Python
entry point regardless of which command is invoked.
"""

import platform
import sys


def assert_supported_platform() -> None:
    """Raise SystemExit with a clear message if running on Windows."""
    if platform.system() == "Windows":
        print(
            "error: Quaid requires macOS or Linux. Windows is not supported.\n"
            "See https://github.com/quaid-labs/quaid for platform requirements.",
            file=sys.stderr,
        )
        sys.exit(1)


# Run the check at import time so any Python entry point catches it immediately.
assert_supported_platform()
