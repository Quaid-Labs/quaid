from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

from lib.runtime_context import get_workspace_dir


def get_runtime_root(workspace: Optional[Union[str, Path]] = None) -> Path:
    explicit = (
        str(os.environ.get("MEMORY_RUNTIME_DIR", "") or "").strip()
        or str(os.environ.get("RUNTIME_DIR", "") or "").strip()
    )
    if explicit:
        return Path(explicit).expanduser().resolve()
    if workspace is None:
        base = Path(get_workspace_dir()).resolve()
    else:
        base = Path(workspace).expanduser().resolve()
    return base / ".runtime"
