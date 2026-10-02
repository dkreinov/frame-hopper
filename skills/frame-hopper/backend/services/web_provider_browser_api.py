"""Loopback browser API for human-signed-in website video accounts.

The shared browser profile and bearer token live in the user's application data,
outside the project and repository. This API uses the public website UI only.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import re
import secrets
from typing import Any, Literal
from urllib.parse import urlparse, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from backend.services.web_provider_gateway import WebProviderGateway
from backend.services.web_provider_browser_state import private_state_dir
from backend.services.web_provider_catalog import BY_NAME, MODEL_FAMILIES, PROVIDERS, REVIEWED_ON, select_providers


PROVIDER_URLS = {provider.name: provider.url for provider in PROVIDERS}
PROVIDER_HOSTS = {provider.name: provider.hosts for provider in PROVIDERS}
SIGNED_OUT_MARKERS = {
    "dreamina": ("Sign in to start creating", "Sign in to Dreamina"),
}
BLOCKED_CLICK = re.compile(
    r"\b(generate|create video|submit|subscribe|checkout|purchase|buy|pay|upgrade|delete)\b",
    re.IGNORECASE,
)
UPLOAD_CONTROL = re.compile(r"\b(upload|frame|image|reference|photo)\b", re.IGNORECASE)


def _provider(name: str) -> str:
    if name not in PROVIDER_URLS:
        raise HTTPException(404, "unknown provider")
    return name


def _site_url(provider: str, url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or port not in {None, 443} or not any(
        host == allowed or host.endswith("." + allowed)
        for allowed in PROVIDER_HOSTS[provider]
    )):
        raise HTTPException(400, "navigation must stay on the selected provider's HTTPS site")
    return url


def _safe_url(url: str) -> str:
    """Never expose OAuth query parameters or fragments in API responses."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class NavigateRequest(BaseModel):
    url: str | None = None


class ActionRequest(BaseModel):
    kind: Literal["click", "fill", "select", "press", "dismiss"]
    selector: str = Field(min_length=1, max_length=500)
    value: str | None = Field(default=None, max_length=20000)


class InspectRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=500)


class StageRequest(BaseModel):
    project: Path
    source_paths: dict[str, Path]
    selectors_by_source: dict[str, str]


class SubmitRequest(BaseModel):
    project: Path
    selector: str = Field(min_length=1, max_length=500)
    visible_recheck: dict[str, Any]


class DownloadRequest(BaseModel):
    project: Path
    selector: str = Field(min_length=1, max_length=500)


class BrowserManager:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.playwright: Any = None
        self.context: Any = None
        self.pages: dict[str, Any] = {}
        self.navigation_wait_result: dict[str, str] = {}
        self.staged: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {}

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.playwright is not None:
            await self.playwright.stop()

    def _context_closed(self) -> None:
        self.context = None
        self.pages.clear()
        self.navigation_wait_result.clear()
        self.staged.clear()

    async def open(self, provider: str, url: str | None = None) -> dict[str, Any]:
        provider = _provider(provider)
        target = _site_url(provider, url or PROVIDER_URLS[provider])
        profile = self.state_dir / "profiles" / "shared"
        if self.context is None:
            profile.mkdir(parents=True, exist_ok=True)
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=False,
                accept_downloads=True, viewport={"width": 1440, "height": 900},
            )
            self.context.on("close", lambda *_: self._context_closed())
        page = self.pages.get(provider)
        created = page is None or page.is_closed()
        if created:
            # Reuse the managed page when switching providers. This keeps heavy
            # video sites from accumulating in nine live Chrome tabs while the
            # persistent context retains their cookies and local storage.
            page = next((other for other in self.pages.values()
                         if not other.is_closed()), None)
            if page is None:
                page = next((candidate for candidate in self.context.pages
                             if candidate.url == "about:blank" and not candidate.is_closed()), None)
            if page is None:
                page = await self.context.new_page()
            self.pages = {provider: page}
            self.staged.clear()
        navigation_wait_result = self.navigation_wait_result.get(provider, "not_requested")
        try:
            _site_url(provider, page.url)
        except HTTPException:
            outside_provider = True
        else:
            outside_provider = False
        retry_timed_out = outside_provider and navigation_wait_result == "timed_out"
        if created or retry_timed_out or (url is not None and page.url != target):
            # A site's scripts can keep DOMContentLoaded from arriving for a long time.
            # A committed response is enough to hand the visible tab to the user.
            try:
                await page.goto(target, wait_until="commit", timeout=15000)
            except PlaywrightTimeoutError:
                navigation_wait_result = "timed_out"
            else:
                navigation_wait_result = "committed"
            self.navigation_wait_result[provider] = navigation_wait_result
            self.staged = {job_id: receipt for job_id, receipt in self.staged.items()
                           if receipt[0] != provider}
        return {"provider": provider, "url": _safe_url(page.url),
                "profile": str(profile), "navigation_wait_result": navigation_wait_result}

    def page(self, provider: str) -> Any:
        _provider(provider)
        if provider not in self.pages or self.pages[provider].is_closed():
            raise HTTPException(409, "open the provider browser first")
        return self.pages[provider]

    def provider_page(self, provider: str) -> Any:
        page = self.page(provider)
        _site_url(provider, page.url)
        return page

    @staticmethod
    def account_status(provider: str, visible_text: str) -> str:
        if any(marker in visible_text for marker in SIGNED_OUT_MARKERS.get(provider, ())):
            return "signed_out"
        return "unverified"

    async def require_account_check(self, provider: str, page: Any) -> None:
        visible_text = await page.locator("body").inner_text(timeout=10000)
        if self.account_status(provider, visible_text) == "signed_out":
            raise HTTPException(409, "provider page requires sign-in")

    async def status(self, provider: str) -> dict[str, Any]:
        _provider(provider)
        page = self.pages.get(provider)
        browser_open = page is not None and not page.is_closed()
        status = "not_open"
        current_url = None
        if browser_open:
            current_url = _safe_url(page.url)
            try:
                _site_url(provider, page.url)
            except HTTPException:
                status = "loading" if page.url == "about:blank" and self.navigation_wait_result.get(provider) == "timed_out" else "outside_provider"
            else:
                try:
                    body = await page.locator("body").inner_text(timeout=3000)
                except PlaywrightTimeoutError:
                    status = "loading"
                else:
                    status = self.account_status(provider, body)
        return {
            "provider": provider,
            "browser_open": browser_open,
            "profile_exists": (self.state_dir / "profiles" / "shared").exists(),
            "legacy_provider_profile_exists": (self.state_dir / "profiles" / provider).exists(),
            "account_status": status,
            "navigation_wait_result": self.navigation_wait_result.get(provider, "not_requested"),
            "url": current_url,
            "session_scope": "shared_local_chrome_profile",
            "shares_google_cookies_with_other_providers": True,
            "shares_in_app_browser_session": False,
            "start_end_frame_support": ("advertised_unverified_for_this_account"
                                        if BY_NAME[provider].browser_support == "guarded_video_job"
                                        else "not_calibrated"),
            "browser_support": BY_NAME[provider].browser_support,
            "generation_calibration": "unverified",
        }

    async def snapshot(self, provider: str) -> dict[str, Any]:
        page = self.provider_page(provider)
        # No form values, cookies, local storage, request headers, or screenshots.
        controls = await page.locator("button, a, input, select, textarea, [role=button]").evaluate_all(
            """nodes => nodes.slice(0, 250).map((n, i) => ({
                index: i, tag: n.tagName.toLowerCase(), role: n.getAttribute('role'),
                type: n.getAttribute('type'), label: (n.getAttribute('aria-label') ||
                  n.getAttribute('placeholder') || n.innerText || '').trim().slice(0, 160),
                disabled: !!n.disabled
            }))"""
        )
        body = await page.locator("body").inner_text(timeout=10000)
        return {
            "provider": provider, "url": _safe_url(page.url), "title": await page.title(),
            "account_status": self.account_status(provider, body),
            "visible_text": body[:12000], "controls": controls,
        }

    async def screenshot(self, provider: str) -> dict[str, str]:
        page = self.provider_page(provider)
        destination = self.state_dir / "snapshots" / f"{provider}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(destination), full_page=False, timeout=15000)
        return {"provider": provider, "path": str(destination)}

    async def inspect(self, provider: str, selector: str) -> dict[str, Any]:
        page = self.provider_page(provider)
        locator = page.locator(selector)
        count = await locator.count()
        if count > 30:
            raise HTTPException(400, "selector matched too many elements")
        matches = await locator.evaluate_all("""nodes => nodes.map(n => ({
            tag: n.tagName.toLowerCase(), text: (n.innerText || '').trim().slice(0, 300),
            className: typeof n.className === 'string' ? n.className.slice(0, 300) : '',
            ariaLabel: n.getAttribute('aria-label'), title: n.getAttribute('title'),
            type: n.getAttribute('type'), role: n.getAttribute('role')
        }))""")
        return {"provider": provider, "count": count, "matches": matches}

    async def action(self, provider: str, request: ActionRequest) -> dict[str, str]:
        page = self.provider_page(provider)
        locator = page.locator(request.selector)
        if await locator.count() != 1:
            raise HTTPException(409, "selector must match exactly one element")
        if request.kind == "click":
            element_type = (await locator.get_attribute("type") or "").lower()
            descriptor = " ".join(filter(None, [
                await locator.inner_text(timeout=5000),
                await locator.get_attribute("aria-label"),
                await locator.get_attribute("title"),
                await locator.get_attribute("value"),
            ]))
            if element_type == "submit" or not descriptor.strip() or BLOCKED_CLICK.search(descriptor):
                raise HTTPException(409, "submission or payment click needs the guarded job endpoint")
            await locator.click(timeout=10000)
        elif request.kind == "dismiss":
            class_name = await locator.get_attribute("class") or ""
            if "close" not in class_name.lower() or await locator.locator("xpath=ancestor::*[contains(@class, 'modal')]").count() == 0:
                raise HTTPException(409, "dismiss requires a close control inside a modal")
            await locator.click(timeout=10000)
        elif request.kind == "fill":
            if request.value is None:
                raise HTTPException(400, "fill needs a value")
            if (await locator.get_attribute("type") or "").lower() == "password":
                raise HTTPException(409, "enter account passwords directly in Chrome")
            await locator.fill(request.value, timeout=10000)
        elif request.kind == "select":
            if request.value is None:
                raise HTTPException(400, "select needs a value")
            await locator.select_option(label=request.value, timeout=10000)
        else:
            if request.value != "Escape":
                raise HTTPException(400, "only Escape is allowed through press")
            await page.keyboard.press("Escape")
        self.staged = {job_id: receipt for job_id, receipt in self.staged.items()
                       if receipt[0] != provider}
        return {"url": _safe_url(page.url), "action": request.kind}

    async def stage(self, job_id: str, request: StageRequest) -> list[dict[str, str]]:
        gateway = WebProviderGateway(request.project)
        job = gateway.get_job(job_id)
        page = self.provider_page(job.request["website"])
        await self.require_account_check(job.request["website"], page)
        manifest = gateway.stage_manifest(job_id, request.source_paths)
        if set(request.selectors_by_source) != {item["source_id"] for item in manifest}:
            raise HTTPException(400, "selectors must cover each media source exactly")
        if len(set(request.selectors_by_source.values())) != len(manifest):
            raise HTTPException(400, "each media source needs a distinct upload control")
        self.staged = {other_id: receipt for other_id, receipt in self.staged.items()
                       if receipt[0] != job.request["website"]}
        for item in manifest:
            locator = page.locator(request.selectors_by_source[item["source_id"]])
            if await locator.count() != 1:
                raise HTTPException(409, f"selector for {item['source_id']} must match one file input")
            path = Path(item["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise HTTPException(409, "media changed before upload")
            if (await locator.get_attribute("type") or "").lower() == "file":
                await locator.set_input_files(str(path), timeout=30000)
            else:
                descriptor = " ".join(filter(None, [
                    await locator.inner_text(timeout=5000),
                    await locator.get_attribute("id"),
                    await locator.get_attribute("class"),
                    await locator.get_attribute("aria-label"),
                    await locator.get_attribute("title"),
                ]))
                if BLOCKED_CLICK.search(descriptor) or not UPLOAD_CONTROL.search(descriptor):
                    raise HTTPException(409, "selector must identify an upload control")
                async with page.expect_file_chooser(timeout=10000) as pending:
                    await locator.click(timeout=10000)
                await (await pending.value).set_files(str(path), timeout=30000)
        self.staged[job_id] = (
            job.request["website"], page.url,
            tuple((item["source_id"], item["sha256"]) for item in manifest),
        )
        return [{"role": item["role"], "source_id": item["source_id"], "sha256": item["sha256"]} for item in manifest]

    async def submit(self, job_id: str, request: SubmitRequest) -> dict[str, str]:
        gateway = WebProviderGateway(request.project)
        job = gateway.get_job(job_id)
        page = self.provider_page(job.request["website"])
        receipt = self.staged.get(job_id)
        expected_sources = tuple(
            (source_id, job.request["source_sha256"][source_id])
            for source_id in job.request["source_ids"]
        )
        if receipt != (job.request["website"], page.url, expected_sources):
            raise HTTPException(409, "stage the exact media in this browser page before submission")
        await self.require_account_check(job.request["website"], page)
        locator = page.locator(request.selector)
        if await locator.count() != 1:
            raise HTTPException(409, "submit selector must match exactly one element")
        if not await locator.is_visible() or not await locator.is_enabled():
            raise HTTPException(409, "submit control is not actionable")
        screenshot = await page.screenshot(full_page=False, timeout=15000)
        screenshot_sha256 = hashlib.sha256(screenshot).hexdigest()
        evidence_ref = f"pre_submit_{job_id}_{screenshot_sha256[:16]}"
        evidence_path = self.state_dir / "evidence" / f"{evidence_ref}.png"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        if evidence_path.exists() and evidence_path.read_bytes() != screenshot:
            raise HTTPException(409, "pre-submit evidence collision")
        evidence_path.write_bytes(screenshot)
        recheck = dict(request.visible_recheck)
        recheck["evidence_ref"] = evidence_ref
        recheck["browser_screenshot_sha256"] = screenshot_sha256
        # Persist the uncertain state before the irreversible website click.
        gateway.mark_submission_started(job_id, recheck)
        await locator.click(timeout=15000)
        return {"job_id": job_id, "state": "unknown", "url": _safe_url(page.url),
                "evidence_ref": evidence_ref}

    async def download(self, job_id: str, request: DownloadRequest) -> dict[str, str]:
        gateway = WebProviderGateway(request.project)
        job = gateway.get_job(job_id)
        if job.state not in {"submitted", "running", "completed"} or not job.website_job_id:
            raise HTTPException(409, "record the website history job ID before download")
        page = self.provider_page(job.request["website"])
        locator = page.locator(request.selector)
        if await locator.count() != 1:
            raise HTTPException(409, "download selector must match exactly one element")
        destination = self.state_dir / "downloads" / f"{job_id}.mp4"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise HTTPException(409, "download path already exists; inspect it before retrying")
        async with page.expect_download(timeout=60000) as pending:
            await locator.click(timeout=15000)
        download = await pending.value
        await download.save_as(str(destination))
        return {
            "job_id": job_id, "path": str(destination),
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        }


def create_app(state_dir: Path | None = None) -> FastAPI:
    root = state_dir or private_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    token_file = root / "access.token"
    try:
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(40))
    token = token_file.read_text(encoding="ascii").strip()
    manager = BrowserManager(root)
    operation_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(title="Olga Movie Web Provider Browser", version="1.0", lifespan=lifespan)
    app.state.manager = manager
    app.state.token_file = token_file

    @app.exception_handler(PlaywrightError)
    async def browser_error(_request, error: PlaywrightError) -> JSONResponse:
        summary = str(error).split("Call log:", 1)[0].strip()[:400]
        return JSONResponse(status_code=409, content={"detail": summary})

    def authorized(x_gateway_token: str | None = Header(default=None)) -> None:
        if x_gateway_token is None or not secrets.compare_digest(x_gateway_token, token):
            raise HTTPException(401, "gateway token required")

    @app.get("/v1/models", dependencies=[Depends(authorized)])
    def models() -> dict[str, Any]:
        return {"model_families": list(MODEL_FAMILIES), "reviewed_on": REVIEWED_ON}

    @app.get("/v1/session", dependencies=[Depends(authorized)])
    async def session() -> dict[str, Any]:
        async with operation_lock:
            active = next((name for name, page in manager.pages.items()
                           if not page.is_closed()), None)
            return {"active_provider": active, "staged_job_count": len(manager.staged)}

    @app.get("/v1/providers", dependencies=[Depends(authorized)])
    def providers(model: str | None = None, media: str | None = None,
                  free_only: bool = False) -> dict[str, Any]:
        try:
            selected = select_providers(model=model, media=media, free_only=free_only)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return {"reviewed_on": REVIEWED_ON, "providers": [{
            "name": item.name, "url": item.url, "open": item.name in manager.pages,
            "media": list(item.media), "model_access": item.models,
            "free_offer": item.free_offer, "free_cadence": item.cadence,
            "source": item.source, "browser_support": item.browser_support,
            "limitation": item.limitation,
            "start_end_frame_support": ("advertised_unverified_for_this_account"
                                         if item.browser_support == "guarded_video_job"
                                         else "not_calibrated"),
        } for item in selected]}

    @app.get("/v1/providers/{provider}/status", dependencies=[Depends(authorized)])
    async def provider_status(provider: str) -> dict[str, Any]:
        async with operation_lock:
            return await manager.status(provider)

    @app.post("/v1/providers/{provider}/open", dependencies=[Depends(authorized)])
    async def open_provider(provider: str, request: NavigateRequest) -> dict[str, Any]:
        async with operation_lock:
            return await manager.open(provider, request.url)

    @app.get("/v1/providers/{provider}/snapshot", dependencies=[Depends(authorized)])
    async def snapshot(provider: str) -> dict[str, Any]:
        async with operation_lock:
            return await manager.snapshot(provider)

    @app.get("/v1/providers/{provider}/screenshot", dependencies=[Depends(authorized)])
    async def screenshot(provider: str) -> dict[str, str]:
        async with operation_lock:
            return await manager.screenshot(provider)

    @app.post("/v1/providers/{provider}/inspect", dependencies=[Depends(authorized)])
    async def inspect(provider: str, request: InspectRequest) -> dict[str, Any]:
        async with operation_lock:
            return await manager.inspect(provider, request.selector)

    @app.post("/v1/providers/{provider}/action", dependencies=[Depends(authorized)])
    async def action(provider: str, request: ActionRequest) -> dict[str, str]:
        async with operation_lock:
            return await manager.action(provider, request)

    @app.post("/v1/jobs/{job_id}/stage", dependencies=[Depends(authorized)])
    async def stage(job_id: str, request: StageRequest) -> dict[str, Any]:
        async with operation_lock:
            return {"staged": await manager.stage(job_id, request)}

    @app.post("/v1/jobs/{job_id}/submit", dependencies=[Depends(authorized)])
    async def submit(job_id: str, request: SubmitRequest) -> dict[str, str]:
        async with operation_lock:
            return await manager.submit(job_id, request)

    @app.post("/v1/jobs/{job_id}/download", dependencies=[Depends(authorized)])
    async def download(job_id: str, request: DownloadRequest) -> dict[str, str]:
        async with operation_lock:
            return await manager.download(job_id, request)

    return app
