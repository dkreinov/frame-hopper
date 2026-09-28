"""Private local state location shared by the browser service and its client."""

from __future__ import annotations

import os
from pathlib import Path


def private_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "olga_movie" / "web_provider_browser"
