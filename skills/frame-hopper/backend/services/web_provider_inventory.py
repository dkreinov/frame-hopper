"""Read-only credit observations for the website provider inventory.

Website text is not proof of account ownership, free-credit eligibility, or a
model quote. The report therefore keeps observations separate from estimates.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Callable, Iterable, Mapping

from backend.services.web_provider_catalog import FREE_POSSIBLE, MODEL_MEDIA


NUMBER = r"(?P<number>\d{1,7}(?:,\d{3})*)"
BALANCE_PATTERNS = (
    re.compile(rf"\b(?P<label>(?:free\s+)?credits?\s+(?:remaining|left|available|balance))\s*[:：-]?\s*{NUMBER}\b", re.I),
    re.compile(rf"\b(?P<label>(?:remaining|available|balance)\s+(?:free\s+)?credits?)\s*[:：-]?\s*{NUMBER}\b", re.I),
    re.compile(rf"\b{NUMBER}\s+(?P<label>(?:free\s+)?credits?\s+(?:remaining|left|available))\b", re.I),
    re.compile(rf"^\s*(?P<label>(?:free\s+)?credit\s+balance)\s*[:：-]?\s*{NUMBER}\s*$", re.I),
    re.compile(rf"^\s*(?P<label>(?:free\s+)?credits?)\s*[:：-]\s*{NUMBER}\s*$", re.I),
)


def visible_balance_candidates(text: str) -> dict[str, Any]:
    """Extract explicit balance-like labels, excluding daily offer copy.

    These are unverified observations. Conflicting amounts remain ambiguous.
    """
    amounts: dict[str, set[int]] = {"free": set(), "unspecified": set()}
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) > 160:
            continue
        for pattern in BALANCE_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            label = match.group("label").lower()
            bucket = "free" if "free" in label else "unspecified"
            amounts[bucket].add(int(match.group("number").replace(",", "")))
            break
    return {
        "visible_free_credits": next(iter(amounts["free"])) if len(amounts["free"]) == 1 else None,
        "visible_unspecified_credits": (next(iter(amounts["unspecified"]))
                                          if len(amounts["unspecified"]) == 1 else None),
        "balance_status": ("ambiguous" if any(len(values) > 1 for values in amounts.values())
                           else "observed_unverified" if any(amounts.values()) else "not_found"),
    }


def inventory_rows(providers: Iterable[Mapping[str, Any]],
                   observations: Mapping[str, Mapping[str, Any]] | None = None,
                   *, model: str | None = None, media: str | None = None,
                   free_only: bool = False) -> list[dict[str, Any]]:
    """One row per site/model. Site credits are shared, never model balances."""
    observations = observations or {}
    model_filter = model.strip().lower() if model else None
    rows = []
    for provider in providers:
        observation = observations.get(provider["name"], {})
        for family, access in provider["model_access"].items():
            if model_filter is not None and family != model_filter:
                continue
            if media and MODEL_MEDIA.get(family, "video") != media:
                continue
            if free_only and access not in FREE_POSSIBLE:
                continue
            rows.append({
                "site": provider["name"], "model": family,
                "model_free_access": access,
                "free_cadence": provider["free_cadence"],
                "advertised_offer": provider["free_offer"],
                "account_status": observation.get("account_status", "not_scanned"),
                "balance_status": observation.get("balance_status", "not_scanned"),
                "visible_free_credits": observation.get("visible_free_credits"),
                "visible_unspecified_credits": observation.get("visible_unspecified_credits"),
                "credit_pool": "shared_by_site",
                "exact_model_cost": None,
                "generations_possible": None,
                "source": provider["source"],
                "limitation": provider["limitation"],
            })
    return rows


def scan_visible_balances(browser: Any, providers: Iterable[Mapping[str, Any]],
                          *, on_site: Callable[[str], None] | None = None) -> dict[str, dict[str, Any]]:
    """Visit sites in the one API tab, refusing to invalidate staged media."""
    session = browser.session()
    if session["staged_job_count"]:
        raise RuntimeError("cannot scan while a browser job has staged uploads")
    original = session["active_provider"]
    original_url = None
    if original:
        previous = browser.status(original)
        if previous["account_status"] == "outside_provider":
            raise RuntimeError("cannot scan during an account sign-in or external page")
        original_url = previous.get("url")
    observations: dict[str, dict[str, Any]] = {}
    try:
        for provider in providers:
            name = provider["name"]
            if on_site:
                on_site(name)
            try:
                browser.open(name)
                status = browser.status(name)
                account = status["account_status"]
                if account in {"outside_provider", "loading", "signed_out", "not_open"}:
                    observations[name] = {"account_status": account, "balance_status": "not_available"}
                    continue
                snapshot = browser.snapshot(name)
                observations[name] = {
                    "account_status": snapshot["account_status"],
                    **visible_balance_candidates(snapshot["visible_text"]),
                }
            except Exception as error:
                observations[name] = {"account_status": "scan_error",
                                      "balance_status": "not_available",
                                      "error_type": type(error).__name__}
    finally:
        if original:
            try:
                browser.open(original, original_url)
            except Exception:
                pass
    return observations


def inventory_report(providers: Iterable[Mapping[str, Any]],
                     observations: Mapping[str, Mapping[str, Any]] | None = None,
                     *, model: str | None = None, media: str | None = None,
                     free_only: bool = False) -> dict[str, Any]:
    return {"observed_at": datetime.now(timezone.utc).isoformat(),
            "credit_semantics": "Credits are shared by site; model costs and usable generations need a live exact quote.",
            "rows": inventory_rows(providers, observations, model=model, media=media,
                                   free_only=free_only)}
