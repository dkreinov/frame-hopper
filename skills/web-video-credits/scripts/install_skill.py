"""Copy this skill, including its API code, to the user's Codex skills folder."""

from __future__ import annotations

import os
from pathlib import Path
import shutil


def main() -> None:
    source = Path(__file__).resolve().parents[1]
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    destination = codex_home / "skills" / source.name
    if source.resolve() == destination.resolve():
        print(destination)
        return
    if destination.exists() and not destination.is_dir():
        raise RuntimeError(f"skill destination is not a directory: {destination}")
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache", ".venv", "*.pyc", "*.sqlite3", "access.token", "profiles",
    ))
    print(destination)


if __name__ == "__main__":
    main()
