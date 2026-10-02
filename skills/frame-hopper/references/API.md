# Local browser API

Run commands from this skill's directory after installing `requirements.txt` and Google Chrome. `WebProviderBrowserClient.ensure_running()` starts the loopback service at `127.0.0.1:8765` if needed; alternatively run `python -m tools.cli.web_provider_browser_api --port 8765`. The service saves its token and persistent Chrome profile under local application data (`olga_movie/web_provider_browser`); these files remain private to the machine.

## Browser client

```python
from backend.services.web_provider_browser_client import WebProviderBrowserClient

browser = WebProviderBrowserClient()
browser.ensure_running()
browser.models()                # curated model families
browser.sites_for_model("kling") # possible free Kling sites only
browser.providers(model="kling") # every listed Kling site, with model-specific access
browser.providers(media="audio", free_only=True)
browser.providers()             # complete catalog
browser.open("dreamina")       # reuse the one active tab
browser.status("dreamina")
browser.snapshot("dreamina")
browser.screenshot("dreamina")
browser.inspect("dreamina", "button")
```

`providers(model=..., media=..., free_only=...)` filters the dated catalog by advertised model family and model-specific free access. `free_only` includes conditional promotions, so it does not establish available credits in this account. The HTTP routes are `GET /v1/models` and `GET /v1/providers?model=kling&free_only=true` (or `media=audio`). Read [current provider research](PROVIDER_RESEARCH.md) for source links and limitations. Newly added sites have navigation and inspection only; audio generation is not implemented.

`status` reports this Chrome profile's state. `unverified` never proves login. `snapshot` exposes visible text and form labels, omitting input values, cookies, storage, and headers. `action(provider, kind, selector, value)` supports navigation clicks, fills, selects, and key presses; it rejects Generate and payment controls. Provider navigation stays on the selected HTTPS domain. User sign-in happens directly in the Chrome window.

The browser client also has `stage`, `submit`, and `download` methods. They require a prepared job in `WebProviderGateway`, which persists a local SQLite ledger in the chosen project directory.

## Prepare and stage two images

Create a `FamilyShotPlan` JSON using `backend.services.family_shots.FamilyShotPlan`. Its shot lists sources with distinct `start` and `end` roles. `local_path` values are relative to a source root, but pass the resolved source paths to the gateway. The gateway hashes both files and freezes the request.

```python
from pathlib import Path
from backend.services.family_shots import FamilyShotPlan
from backend.services.web_provider_gateway import WebProviderGateway

plan = FamilyShotPlan.model_validate_json(Path("plan.json").read_text(encoding="utf-8"))
project = Path("projects/my-film")
gateway = WebProviderGateway(project)
paths = {"start_photo": Path("/absolute/start.jpg"), "end_photo": Path("/absolute/end.jpg")}
job = gateway.prepare(plan, "shot_id", paths, website="dreamina",
                      requested_model="exact model shown on site", controls={},
                      free_credits_only=True)
```

Inspect the signed-in live form and record the exact model, selected controls, displayed cost, free balance, and evidence with `gateway.observe(job.job_id, observation)`, then `gateway.approve(job.job_id, observation_sha256, approved_by=...)`. Read `web_provider_adapters.py` for the observation fields and provider requirements; an unsupported or unclear setting stays blocked. The ledger approval records the observed state; it is not a substitute for user authorization.

Stage only after the live page shows separate upload controls. Each source ID must map to its own verified selector:

```python
browser.stage(job.job_id, project, paths,
              {"start_photo": "selector for first frame", "end_photo": "selector for last frame"})
```

Staging is a real website upload. Switching pages or changing form controls invalidates the stage. Before `browser.submit(job.job_id, project, generate_selector, visible_recheck)`, recheck the same model, settings, exact price, and sufficient **free** balance. `submit` persists an `unknown` outcome before clicking Generate; never blindly retry it. Resolve the website history and unique website job ID with the gateway before a retry. `browser.download` saves an MP4 in private application data; `gateway.import_result` verifies source and output hashes before importing it.

All HTTP endpoints require `X-Gateway-Token`. The Python client reads it from the private state directory. Do not print, copy, or publish it.
