"""The shared browser API keeps account state local and guards website clicks."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
import pytest

from backend.services.web_provider_browser_api import (
    ActionRequest, BrowserManager, StageRequest, SubmitRequest, _safe_url, _site_url, create_app,
)
from backend.services.web_provider_browser_client import WebProviderBrowserClient
from backend.services.web_provider_gateway import WebProviderGateway
from backend.services.family_shots import FamilyShotPlan


def test_loopback_api_requires_its_private_token(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/providers").status_code == 401
        token = (tmp_path / "access.token").read_text(encoding="ascii").strip()
        response = client.get("/v1/providers", headers={"X-Gateway-Token": token})
        session = client.get("/v1/session", headers={"X-Gateway-Token": token})
    assert response.status_code == 200
    assert {item["name"] for item in response.json()["providers"]} == {
        "kling", "dreamina", "pixverse", "vidu", "flow", "krea",
        "seaart", "openart", "hailuo", "runway", "luma", "leonardo",
        "firefly", "magnific", "elevenlabs", "cartesia", "fish-audio",
        "minimax-audio", "suno",
    }
    assert session.json() == {"active_provider": None, "staged_job_count": 0}


def test_model_filter_tracks_model_specific_free_access(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        token = (tmp_path / "access.token").read_text(encoding="ascii").strip()
        headers = {"X-Gateway-Token": token}
        all_kling = client.get("/v1/providers?model=kling", headers=headers)
        free_kling = client.get("/v1/providers?model=kling&free_only=true", headers=headers)
        audio = client.get("/v1/providers?media=audio&free_only=true", headers=headers)
        mismatched = client.get("/v1/providers?model=elevenlabs&media=video", headers=headers)
        models = client.get("/v1/models", headers=headers)
        invalid = client.get("/v1/providers?model=retired-model", headers=headers)
    assert all_kling.status_code == free_kling.status_code == audio.status_code == models.status_code == 200
    assert {item["name"] for item in all_kling.json()["providers"]} == {
        "kling", "pixverse", "krea", "openart", "luma", "firefly", "magnific",
    }
    assert [item["name"] for item in free_kling.json()["providers"]] == ["kling"]
    assert free_kling.json()["providers"][0]["model_access"]["kling"] == "conditional"
    assert {item["name"] for item in audio.json()["providers"]} == {
        "firefly", "elevenlabs", "cartesia", "fish-audio", "minimax-audio", "suno",
    }
    assert "elevenlabs" in models.json()["model_families"]
    assert mismatched.json()["providers"] == []
    assert invalid.status_code == 400


def test_status_does_not_confuse_iab_signin_with_local_profile(tmp_path: Path) -> None:
    manager = BrowserManager(tmp_path)
    status = asyncio.run(manager.status("seaart"))
    assert status["account_status"] == "not_open"
    assert status["profile_exists"] is False
    assert status["shares_in_app_browser_session"] is False
    assert status["shares_google_cookies_with_other_providers"] is True
    assert status["generation_calibration"] == "unverified"


def test_provider_switch_reuses_one_page_and_profile_without_moving_legacy_profiles(tmp_path: Path) -> None:
    legacy = tmp_path / "profiles" / "dreamina" / "sentinel.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("preserved", encoding="utf-8")

    class FakePage:
        def __init__(self) -> None:
            self.url = "about:blank"
            self.navigation_kwargs = None

        def is_closed(self) -> bool:
            return False

        async def goto(self, url: str, **kwargs) -> None:
            self.url = url
            self.navigation_kwargs = kwargs

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        def on(self, _event: str, _callback) -> None:
            pass

        async def new_page(self) -> FakePage:
            page = FakePage()
            self.pages.append(page)
            return page

    class FakeChromium:
        def __init__(self) -> None:
            self.calls = []
            self.context = FakeContext()

        async def launch_persistent_context(self, path: str, **_kwargs) -> FakeContext:
            self.calls.append(path)
            return self.context

    chromium = FakeChromium()
    manager = BrowserManager(tmp_path)
    manager.playwright = SimpleNamespace(chromium=chromium)

    async def run() -> None:
        first = await manager.open("dreamina")
        dreamina_page = manager.pages["dreamina"]
        manager.staged["pending"] = ("dreamina", dreamina_page.url, ())
        second = await manager.open("seaart")
        assert manager.pages["seaart"] is dreamina_page
        assert manager.staged == {}
        assert (await manager.status("dreamina"))["account_status"] == "not_open"
        await manager.open("dreamina")
        assert first["profile"] == second["profile"] == str(tmp_path / "profiles" / "shared")
        assert len(chromium.calls) == 1
        assert len(chromium.context.pages) == 1
        assert manager.pages == {"dreamina": dreamina_page}
        assert manager.pages["dreamina"].navigation_kwargs == {
            "wait_until": "commit", "timeout": 15000,
        }

    asyncio.run(run())
    assert legacy.read_text(encoding="utf-8") == "preserved"


def test_slow_navigation_returns_pending_status_instead_of_blocking_other_sites(tmp_path: Path) -> None:
    class SlowPage:
        url = "about:blank"

        def is_closed(self) -> bool:
            return False

        async def goto(self, _url: str, **_kwargs) -> None:
            raise PlaywrightTimeoutError("timed out waiting for response")

    page = SlowPage()
    manager = BrowserManager(tmp_path)
    manager.context = SimpleNamespace(pages=[page])

    async def run() -> None:
        opened = await manager.open("flow")
        assert opened["navigation_wait_result"] == "timed_out"
        assert opened["url"] == "about:blank"
        status = await manager.status("flow")
        assert status["account_status"] == "loading"
        assert status["navigation_wait_result"] == "timed_out"

    asyncio.run(run())


def test_timed_out_switch_retries_when_page_remains_on_previous_provider(tmp_path: Path) -> None:
    class Page:
        def __init__(self) -> None:
            self.url = "https://www.vidu.com/create/img2video"
            self.visits = 0

        def is_closed(self) -> bool:
            return False

        async def goto(self, url: str, **_kwargs) -> None:
            self.visits += 1
            if self.visits == 1:
                raise PlaywrightTimeoutError("no response committed")
            self.url = url

    page = Page()
    manager = BrowserManager(tmp_path)
    manager.context = SimpleNamespace(pages=[page])
    manager.pages["vidu"] = page

    async def run() -> None:
        assert (await manager.open("seaart"))["navigation_wait_result"] == "timed_out"
        assert (await manager.status("seaart"))["account_status"] == "outside_provider"
        assert (await manager.open("seaart"))["navigation_wait_result"] == "committed"
        assert page.visits == 2
        assert page.url == "https://www.seaart.ai/"

    asyncio.run(run())


def test_open_keeps_in_progress_oauth_page(tmp_path: Path) -> None:
    class OAuthPage:
        url = "https://accounts.google.com/signin?code=private"

        def is_closed(self) -> bool:
            return False

        async def goto(self, _url: str, **_kwargs) -> None:
            raise AssertionError("open must not interrupt an OAuth sign-in")

    manager = BrowserManager(tmp_path)
    manager.context = SimpleNamespace(pages=[OAuthPage()])
    manager.pages["dreamina"] = manager.context.pages[0]
    manager.navigation_wait_result["dreamina"] = "committed"
    opened = asyncio.run(manager.open("dreamina"))
    assert opened["url"] == "https://accounts.google.com/signin"


def test_browser_close_clears_managed_state_for_relaunch(tmp_path: Path) -> None:
    manager = BrowserManager(tmp_path)
    manager.context = object()
    manager.pages["dreamina"] = object()
    manager.navigation_wait_result["dreamina"] = "committed"
    manager.staged["job"] = ("dreamina", "url", ())
    manager._context_closed()
    assert manager.context is None
    assert manager.pages == {}
    assert manager.navigation_wait_result == {}
    assert manager.staged == {}


def test_status_reports_loading_when_provider_body_is_slow(tmp_path: Path) -> None:
    class SlowBody:
        async def inner_text(self, **_kwargs) -> str:
            raise PlaywrightTimeoutError("body not available")

    class SlowPage:
        url = "https://www.seaart.ai/"

        def is_closed(self) -> bool:
            return False

        def locator(self, _selector: str) -> SlowBody:
            return SlowBody()

    manager = BrowserManager(tmp_path)
    manager.pages["seaart"] = SlowPage()
    status = asyncio.run(manager.status("seaart"))
    assert status["account_status"] == "loading"


def test_oauth_parameters_are_not_returned_as_page_urls() -> None:
    assert _safe_url("https://accounts.google.com/signin?code=secret#token") == (
        "https://accounts.google.com/signin"
    )


@pytest.mark.parametrize("url", [
    "http://dreamina.capcut.com/ai-tool/home",
    "https://dreamina.capcut.com.evil.example/",
    "https://evil.example/",
    "https://user:pass@dreamina.capcut.com/",
    "https://dreamina.capcut.com:444/",
])
def test_navigation_rejects_other_origins(url: str) -> None:
    with pytest.raises(Exception, match="navigation must stay"):
        _site_url("dreamina", url)


def test_dreamina_sign_in_prompt_is_not_reported_as_ready(tmp_path: Path) -> None:
    manager = BrowserManager(tmp_path)
    assert manager.account_status("dreamina", "Sign in to start creating") == "signed_out"
    assert manager.account_status("dreamina", "Free credits 256") == "unverified"


def test_client_rejects_lookalike_loopback_host() -> None:
    with pytest.raises(ValueError, match="loopback"):
        WebProviderBrowserClient("http://127.0.0.1:8765.evil.example")


class _Locator:
    def __init__(self, label: str) -> None:
        self.label = label
        self.clicked = False

    async def count(self) -> int:
        return 1

    async def inner_text(self, **_kwargs) -> str:
        return self.label

    async def get_attribute(self, _name: str) -> None:
        return None

    async def click(self, **_kwargs) -> None:
        self.clicked = True


class _Page:
    url = "https://dreamina.capcut.com/ai-tool/home"

    def __init__(self, locator: _Locator) -> None:
        self.item = locator

    def is_closed(self) -> bool:
        return False

    def locator(self, _selector: str) -> _Locator:
        return self.item


def test_generic_click_cannot_submit_generation(tmp_path: Path) -> None:
    manager = BrowserManager(tmp_path)
    button = _Locator("Generate video · 50 credits")
    manager.pages["dreamina"] = _Page(button)
    with pytest.raises(Exception, match="guarded job endpoint"):
        asyncio.run(manager.action("dreamina", ActionRequest(kind="click", selector="button")))
    assert button.clicked is False


def test_submit_requires_media_staged_in_same_page(tmp_path: Path, monkeypatch) -> None:
    from backend.services import web_provider_browser_api as api

    class _Gateway:
        def __init__(self, _project: Path) -> None:
            pass

        def get_job(self, _job_id: str):
            return SimpleNamespace(request={
                "website": "dreamina", "source_ids": ["first", "last"],
                "source_sha256": {"first": "a", "last": "b"},
            })

        def mark_submission_started(self, _job_id: str, _recheck) -> None:
            raise AssertionError("cannot reach ledger transition before staging")

    monkeypatch.setattr(api, "WebProviderGateway", _Gateway)
    manager = BrowserManager(tmp_path)
    button = _Locator("Generate")
    manager.pages["dreamina"] = _Page(button)
    with pytest.raises(Exception, match="stage the exact media"):
        asyncio.run(manager.submit("web_test", SubmitRequest(
            project=tmp_path, selector="button", visible_recheck={},
        )))
    assert button.clicked is False


def test_stage_binds_each_source_even_when_roles_repeat(tmp_path: Path, monkeypatch) -> None:
    from backend.services import web_provider_browser_api as api

    source_paths = {}
    manifest = []
    for source_id, role in (("first", "start"), ("ref_a", "reference"), ("ref_b", "reference")):
        path = tmp_path / f"{source_id}.png"
        path.write_bytes(source_id.encode())
        source_paths[source_id] = path
        manifest.append({
            "source_id": source_id, "role": role, "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })

    class _Gateway:
        def __init__(self, _project: Path) -> None:
            pass

        def get_job(self, _job_id: str):
            return SimpleNamespace(request={"website": "dreamina"})

        def stage_manifest(self, _job_id: str, _paths):
            return manifest

    class _FileInput:
        def __init__(self) -> None:
            self.path = None

        async def count(self) -> int:
            return 1

        async def get_attribute(self, _name: str) -> str:
            return "file"

        async def set_input_files(self, path: str, **_kwargs) -> None:
            self.path = path

    class _FilePage(_Page):
        def __init__(self) -> None:
            self.inputs = {f"#{source_id}": _FileInput() for source_id in source_paths}

        def locator(self, selector: str):
            if selector == "body":
                return SimpleNamespace(inner_text=self._body_text)
            return self.inputs[selector]

        async def _body_text(self, **_kwargs) -> str:
            return "Fixture account page"

    monkeypatch.setattr(api, "WebProviderGateway", _Gateway)
    manager = BrowserManager(tmp_path)
    page = _FilePage()
    manager.pages["dreamina"] = page
    selectors = {source_id: f"#{source_id}" for source_id in source_paths}
    result = asyncio.run(manager.stage("web_test", StageRequest(
        project=tmp_path, source_paths=source_paths, selectors_by_source=selectors,
    )))
    assert [item["source_id"] for item in result] == list(source_paths)
    assert {source_id: page.inputs[selector].path for source_id, selector in selectors.items()} == {
        source_id: str(path) for source_id, path in source_paths.items()
    }

    duplicate = dict(selectors)
    duplicate["ref_b"] = duplicate["ref_a"]
    with pytest.raises(Exception, match="distinct upload control"):
        asyncio.run(manager.stage("web_test", StageRequest(
            project=tmp_path, source_paths=source_paths, selectors_by_source=duplicate,
        )))


def test_real_browser_twoframe_upload_and_guarded_submit(tmp_path: Path) -> None:
    """Exercise Playwright file inputs and the real free-credit ledger offline."""
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    plan = FamilyShotPlan.model_validate({
        "schema_version": "family-shot-plan/v1",
        "sources": [
            {"source_id": "school", "local_path": "school.png"},
            {"source_id": "birthday", "local_path": "birthday.png"},
        ],
        "shots": [{
            "shot_id": "school_birthday", "mode": "continuous_bridge",
            "provider_route": "web/dreamina",
            "sources": [
                {"source_id": "school", "role": "start"},
                {"source_id": "birthday", "role": "end"},
            ],
            "primary_action": "Move from school to birthday through a continuous doorway wipe",
            "start_state": "Family at school", "end_state": "Family at birthday",
            "timing": {
                "generation_duration_seconds": 5,
                "generation_duration_reason": "Fixture duration",
                "screen_duration_seconds": 5,
                "screen_duration_reason": "Use the whole fixture clip",
            },
        }],
    })
    paths = {name: tmp_path / f"{name}.png" for name in ("school", "birthday")}
    for name, path in paths.items():
        path.write_bytes(name.encode())
    gateway = WebProviderGateway(tmp_path)
    job = gateway.prepare(plan, "school_birthday", paths, website="dreamina",
                          requested_model="Fixture Model", controls={}, free_credits_only=True)
    observation = {
        "calibration_id": "offline-fixture-v1", "displayed_model": "Fixture Model",
        "supported_controls": list(job.request["controls"]),
        "selected_controls": job.request["controls"],
        "displayed_cost": {"amount": "3", "unit": "credits"},
        "displayed_free_balance": {"amount": "5", "unit": "credits"},
        "free_credit_confirmed": True, "evidence_ref": "fixture-form",
        "free_credit_evidence_ref": "fixture-balance",
    }
    job = gateway.observe(job.job_id, observation)
    assert job.state == "ready"
    gateway.approve(job.job_id, job.observation["sha256"], approved_by="fixture")

    async def run() -> None:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:
                pytest.skip(f"local Playwright Chromium unavailable: {exc}")
            try:
                context = await browser.new_context()
                page = await context.new_page()
                html = """<input id='first' type='file'>
                <div id='last-reference-upload'>Last frame upload</div>
                <button id='generate' type='button'>Generate</button>
                <output id='result'></output>
                <script>document.querySelector('#last-reference-upload').onclick = () => {
                  const input = document.createElement('input');
                  input.id = 'last'; input.type = 'file';
                  document.body.appendChild(input); input.click();
                };</script>
                <script>document.querySelector('#generate').onclick = () => {
                  document.querySelector('#result').textContent = 'submitted';
                };</script>"""
                await page.route("https://dreamina.capcut.com/**", lambda route: route.fulfill(
                    status=200, content_type="text/html", body=html,
                ))
                await page.goto("https://dreamina.capcut.com/offline-fixture")
                manager = BrowserManager(tmp_path)
                manager.pages["dreamina"] = page
                await page.set_content("<body>Sign in to Dreamina</body>")
                with pytest.raises(Exception, match="requires sign-in"):
                    await manager.stage(job.job_id, StageRequest(
                        project=tmp_path, source_paths=paths,
                        selectors_by_source={"school": "#first", "birthday": "#last-reference-upload"},
                    ))
                await page.set_content(html)
                staged = await manager.stage(job.job_id, StageRequest(
                    project=tmp_path, source_paths=paths,
                    selectors_by_source={"school": "#first", "birthday": "#last-reference-upload"},
                ))
                assert [item["source_id"] for item in staged] == ["school", "birthday"]
                assert await page.locator("#first").evaluate("e => e.files[0].name") == "school.png"
                assert await page.locator("#last").evaluate("e => e.files[0].name") == "birthday.png"
                with pytest.raises(Exception, match="visible recheck"):
                    await manager.submit(job.job_id, SubmitRequest(
                        project=tmp_path, selector="#generate",
                        visible_recheck={**observation,
                                         "displayed_free_balance": {"amount": "2", "unit": "credits"},
                                         "evidence_ref": "fixture-insufficient"},
                    ))
                assert await page.locator("#result").inner_text() == ""
                assert gateway.get_job(job.job_id).state == "ready"
                response = await manager.submit(job.job_id, SubmitRequest(
                    project=tmp_path, selector="#generate",
                    visible_recheck={**observation, "evidence_ref": "fixture-recheck"},
                ))
                assert response["state"] == "unknown"
                assert await page.locator("#result").inner_text() == "submitted"
                saved = gateway.get_job(job.job_id)
                assert saved.state == "unknown"
                assert (tmp_path / "evidence" / f"{response['evidence_ref']}.png").is_file()
                assert saved.observation["submission_recheck"]["evidence_ref"] == response["evidence_ref"]
            finally:
                await browser.close()

    asyncio.run(run())
