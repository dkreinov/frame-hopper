---
name: frame-hopper
description: Hop between nine video-site pages through a local Chrome API. Inspect account state and free credits, stage start/end frames, and guard submissions with a job ledger; calibrate each live site before use.
---

# FrameHopper

Work from this installed skill's directory, which contains the `backend`, `tools`, and `config` folders. Read [the API reference](references/API.md) before preparing a job.

The Python client starts a local service and reads its private access token:

```python
from backend.services.web_provider_browser_client import WebProviderBrowserClient

browser = WebProviderBrowserClient()
browser.ensure_running()
browser.open("dreamina")
print(browser.status("dreamina"))
print(browser.snapshot("dreamina"))
```

The API drives one active provider tab in a persistent, visible Chrome profile. The account owner completes website sign-in, CAPTCHA, and terms prompts there. A Codex in-app browser sign-in does not transfer. `account_status="unverified"` is not proof of sign-in. Reinspect after switching providers.

For a start/end job, use `WebProviderGateway` to prepare a validated `FamilyShotPlan` with the exact two local images, model, controls, and `free_credits_only=True`. Inspect the current signed-in website form, exact price, and visible free balance. Record a matching observation and approval in the gateway, then use `browser.stage` with separate verified upload controls for each image. Use `browser.submit` only while the current form matches the approved request and visible free credits cover the exact cost. Resolve an uncertain submission in website history before retrying. Download and import a completed result through the gateway.

The nine-site catalog means the sites can be opened; it does not mean their current forms have been calibrated or a generation has succeeded. Check selectors, models, prices, balances, upload behavior, history, and download behavior per site. This is website automation, not an official provider API. Upload media or spend credits only when the current user request authorizes it. Never store credentials, cookies, or the private browser profile in a repository.
