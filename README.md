# Web Video Credits skill

A Codex skill and local Python browser API for inspecting video generation websites through a Chrome profile that you sign into yourself. It lists Kling, Dreamina, PixVerse, Vidu, Google Flow, Krea, SeaArt, OpenArt, and Hailuo. The browser API supports a guarded start/end image job ledger, uploads, submission, and downloads.

**Status:** Browser navigation and the guarded job flow have offline tests. The nine websites have not all been authenticated or verified end to end. Each site's current selectors, models, free balance, exact price, and output flow need live calibration. This uses website pages, not official provider APIs. Website changes may break it.

## Install as a Codex skill

Use the Codex skill installer with GitHub repository `dkreinov/web-video-credits-skill` and path `skills/web-video-credits`, or clone this repository and run:

```bash
python skills/web-video-credits/scripts/install_skill.py
```

The installed skill includes its Python API code. In the installed skill directory, create a Python environment and install the dependencies:

```bash
python -m pip install -r requirements.txt
```

Install Google Chrome on the same machine. The API runs a visible persistent Chrome profile and binds only to `127.0.0.1`. It stores the browser profile and access token in local application data, outside Git. Sign into each website in that Chrome window. A Codex in-app browser sign-in does not transfer to this profile.

## Read-only check

Run from `skills/web-video-credits` in the cloned repository, or from the installed skill directory:

```python
from backend.services.web_provider_browser_client import WebProviderBrowserClient

browser = WebProviderBrowserClient()
print(browser.ensure_running())  # starts the local service if needed
browser.open("dreamina")
print(browser.status("dreamina"))
print(browser.snapshot("dreamina")["visible_text"][:1000])
```

`unverified` account status does not mean signed in. The website can still show a login prompt. For prepared jobs, use `WebProviderGateway` and the methods in [the API reference](skills/web-video-credits/references/API.md). Uploading images and spending credits require authorization for the current task and a live check of the exact free-credit cost.

## Development

From `skills/web-video-credits`, run `python -m pytest tests -q`. Tests use local fixtures and do not spend provider credits.

This repository contains source code and a skill; it contains no browser profile, cookies, access token, account credentials, or user media.
