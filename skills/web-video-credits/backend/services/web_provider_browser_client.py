"""Small local client for agents using the shared visible-browser service."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from backend.services.web_provider_browser_state import private_state_dir


class WebProviderBrowserClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", *,
                 token_file: Path | None = None) -> None:
        parsed = urlparse(base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("browser API client requires a loopback endpoint") from exc
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or port is None or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("browser API client requires a loopback endpoint")
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file or private_state_dir() / "access.token"

    def ensure_running(self, *, timeout_seconds: float = 20) -> dict[str, Any]:
        """Start the loopback service on demand, then return its provider catalog.

        The server keeps authentication in its separate Chrome profile. This
        method does not sign into a provider or copy any browser credentials.
        """
        if self.token_file != private_state_dir() / "access.token":
            raise ValueError("automatic startup requires the default private token path")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        try:
            return self.providers(timeout_seconds=min(2, timeout_seconds))
        except (FileNotFoundError, requests.ConnectionError, requests.Timeout):
            pass
        state_dir = self.token_file.parent
        state_dir.mkdir(parents=True, exist_ok=True)
        port = urlparse(self.base_url).port
        log_path = state_dir / "service.log"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with log_path.open("a", encoding="utf-8") as log:
            subprocess.Popen(
                [sys.executable, "-m", "tools.cli.web_provider_browser_api", "--port", str(port)],
                cwd=Path(__file__).resolve().parents[2],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=flags, start_new_session=os.name != "nt",
            )
        while time.monotonic() < deadline:
            try:
                return self.providers(timeout_seconds=max(0.001, min(2, deadline - time.monotonic())))
            except (FileNotFoundError, requests.ConnectionError, requests.Timeout):
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        raise RuntimeError(f"browser API did not start; inspect {log_path}")

    def _call(self, method: str, path: str, payload: Mapping[str, Any] | None = None,
              *, timeout_seconds: float = 90) -> dict[str, Any]:
        token = self.token_file.read_text(encoding="ascii").strip()
        response = requests.request(
            method, self.base_url + path, json=dict(payload) if payload is not None else None,
            headers={"X-Gateway-Token": token}, timeout=timeout_seconds,
        )
        if not response.ok:
            try:
                detail = response.json().get("detail", "request failed")
            except ValueError:
                detail = "request failed"
            raise requests.HTTPError(
                f"browser API {response.status_code}: {detail}", response=response,
            )
        return response.json()

    def providers(self, *, timeout_seconds: float = 90) -> dict[str, Any]:
        return self._call("GET", "/v1/providers", timeout_seconds=timeout_seconds)

    def status(self, provider: str) -> dict[str, Any]:
        """Return this API's own browser state, not the in-app browser login."""
        return self._call("GET", f"/v1/providers/{provider}/status")

    def open(self, provider: str, url: str | None = None) -> dict[str, Any]:
        return self._call("POST", f"/v1/providers/{provider}/open", {"url": url})

    def snapshot(self, provider: str) -> dict[str, Any]:
        return self._call("GET", f"/v1/providers/{provider}/snapshot")

    def screenshot(self, provider: str) -> Path:
        return Path(self._call("GET", f"/v1/providers/{provider}/screenshot")["path"])

    def inspect(self, provider: str, selector: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/providers/{provider}/inspect", {"selector": selector})

    def action(self, provider: str, kind: str, selector: str,
               value: str | None = None) -> dict[str, Any]:
        return self._call("POST", f"/v1/providers/{provider}/action", {
            "kind": kind, "selector": selector, "value": value,
        })

    def stage(self, job_id: str, project: Path, source_paths: Mapping[str, Path],
              selectors_by_source: Mapping[str, str]) -> dict[str, Any]:
        return self._call("POST", f"/v1/jobs/{job_id}/stage", {
            "project": str(project),
            "source_paths": {key: str(path) for key, path in source_paths.items()},
            "selectors_by_source": dict(selectors_by_source),
        })

    def submit(self, job_id: str, project: Path, selector: str,
               visible_recheck: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"/v1/jobs/{job_id}/submit", {
            "project": str(project), "selector": selector,
            "visible_recheck": dict(visible_recheck),
        })

    def download(self, job_id: str, project: Path, selector: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/jobs/{job_id}/download", {
            "project": str(project), "selector": selector,
        })
