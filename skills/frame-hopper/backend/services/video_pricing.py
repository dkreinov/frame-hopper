"""Dated, conservative video-only estimates; never represent an actual invoice."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[2] / "config" / "video_pricing.json"


def estimate_video_cost(durations: list[int], model: str, *, today: date | None = None) -> dict:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    verified = date.fromisoformat((snapshot["models"].get(model) or {}).get("verified_at", snapshot["verified_at"]))
    age = ((today or date.today()) - verified).days
    entry = snapshot["models"].get(model)
    provider_audio_mode = (entry or {}).get("provider_audio_mode", "disabled")
    result = {"currency": "USD", "model": model, "verified_at": verified.isoformat(),
              "video_only": provider_audio_mode == "disabled",
              "audio": provider_audio_mode != "disabled",
              "provider_audio_mode": provider_audio_mode,
              "actual_invoice": False,
              "base_usd": None, "source": (entry or {}).get("source")}
    if entry is None or age < 0 or age > snapshot["max_age_days"]:
        return {**result, "status": "unavailable", "reason": "Missing or stale price snapshot; reverify provider pricing"}
    allowed_durations = entry.get("allowed_durations")
    if any(
        isinstance(d, bool) or not isinstance(d, int)
        or not entry["duration_min"] <= d <= entry["duration_max"]
        or (allowed_durations is not None and d not in allowed_durations)
        for d in durations
    ):
        raise ValueError("Unsupported duration for priced endpoint")
    seconds = sum(durations)
    rate = entry.get("usd_per_second", entry.get("silent_usd_per_second"))
    rate_basis = "current_snapshot"
    regular_from = entry.get("regular_rate_from")
    regular_rate = entry.get("regular_usd_per_second")
    quote_date = today or date.today()
    if regular_from is not None or regular_rate is not None:
        if not isinstance(regular_from, str) or regular_rate is None:
            return {**result, "status": "unavailable", "reason": "Price snapshot has an incomplete dated rate change"}
        try:
            regular_start = date.fromisoformat(regular_from)
        except ValueError:
            return {**result, "status": "unavailable", "reason": "Price snapshot has an invalid dated rate change"}
        if quote_date >= regular_start:
            rate = regular_rate
            rate_basis = f"regular_from_{regular_start.isoformat()}"
    if rate is None:
        return {**result, "status": "unavailable", "reason": "Price snapshot has no applicable per-second rate"}
    amount = Decimal(seconds) * Decimal(rate)
    return {**result, "status": "estimate", "generated_seconds": seconds,
            "rate_usd_per_second": str(rate), "rate_basis": rate_basis,
            "base_usd": float(amount),
            "scenario_acceptance_rate": 0.6,
            "expected_video_usd_at_scenario_rate": float(round(amount / Decimal("0.6"), 4)),
            "note": "60% acceptance is hypothetical, not measured. Excludes images, planning, review, music, taxes, and overgeneration."}
