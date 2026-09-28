"""Run the shared website browser API on the local machine only."""

from __future__ import annotations

import argparse

import uvicorn

from backend.services.web_provider_browser_api import create_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
