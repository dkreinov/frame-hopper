# FrameHopper

FrameHopper is a Codex skill and local Chrome browser bridge for current video and audio creation sites. It routes by model family, so `sites_for_model("kling")` returns only sites offering Kling with a possible free option; `providers(model="kling")` also shows paid and unverified Kling hosts. The catalog includes ElevenLabs, Cartesia, Fish Audio, MiniMax Audio, Firefly, Runway, Luma, Leonardo, Suno, and Magnific alongside the original video sites. Agents can inspect visible forms; the original video flow tracks guarded start/end-frame jobs through upload, submission, and download.

**Status:** Browser navigation, model filtering, and the guarded video job flow have offline tests. The websites have not all been authenticated or verified end to end. Newly added sites have navigation and inspection only; audio generation and download are not yet implemented. Each site's current selectors, models, free balance, exact price, and output flow need live calibration. This uses website pages, not official provider APIs. Website changes may break it.

## Install as a Codex skill

Use the Codex skill installer with GitHub repository `dkreinov/frame-hopper` and path `skills/frame-hopper`, or clone this repository and run:

```bash
python skills/frame-hopper/scripts/install_skill.py
```

The installed skill includes its Python API code. In the installed skill directory, create a Python environment and install the dependencies:

```bash
python -m pip install -r requirements.txt
```

Install Google Chrome on the same machine. The API runs a visible persistent Chrome profile and binds only to `127.0.0.1`. It stores the browser profile and access token in local application data, outside Git. Sign into each website in that Chrome window. A Codex in-app browser sign-in does not transfer to this profile.

## Read-only check

Run from `skills/frame-hopper` in the cloned repository, or from the installed skill directory:

```python
from backend.services.web_provider_browser_client import WebProviderBrowserClient

browser = WebProviderBrowserClient()
print(browser.ensure_running())  # starts the local service if needed
print(browser.sites_for_model("kling"))
print(browser.providers(media="audio", free_only=True))
browser.open("dreamina")
print(browser.status("dreamina"))
print(browser.snapshot("dreamina")["visible_text"][:1000])
```

`unverified` account status does not mean signed in. The website can still show a login prompt. For prepared jobs, use `WebProviderGateway` and the methods in [the API reference](skills/frame-hopper/references/API.md). Uploading images and spending credits require authorization for the current task and a live check of the exact free-credit cost.

## Credit inventory command

From `skills/frame-hopper` in the clone, or the installed skill directory:

```bash
python -m tools.cli.web_provider_inventory --help
python -m tools.cli.web_provider_inventory --live
python -m tools.cli.web_provider_inventory --model kling --free-only --live
python -m tools.cli.web_provider_inventory --media audio --live --json
```

Omit `--live` for the offline catalog. The live scan navigates the API Chrome profile's one tab; it is separate from Codex's in-app browser. Credit figures are unverified observations until the signed-in form confirms free eligibility. Generation counts require an exact model quote.

## Development

From `skills/frame-hopper`, run `python -m pytest tests -q`. Tests use local fixtures and do not spend provider credits.

This repository contains source code and a skill; it contains no browser profile, cookies, access token, account credentials, or user media.
