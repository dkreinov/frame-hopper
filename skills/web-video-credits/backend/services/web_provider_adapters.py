"""Independent visible-website adapter boundaries with fail-closed calibration."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Protocol


class WebProviderAdapter(Protocol):
    website: str

    def evaluate(self, request: Mapping[str, Any], observed: Mapping[str, Any]) -> str: ...


def _account_checkpoint(observed: Mapping[str, Any]) -> str | None:
    if observed.get("login_required") is True:
        return "needs_login"
    if observed.get("captcha_required") is True or observed.get("terms_required") is True:
        return "needs_human"
    return None


class _CalibratedVisibleAdapter:
    def evaluate(self, request: Mapping[str, Any], observed: Mapping[str, Any]) -> str:
        checkpoint = _account_checkpoint(observed)
        if checkpoint is not None:
            return checkpoint
        if not observed.get("calibration_id"):
            return "needs_calibration"
        if observed.get("displayed_model") != request["requested_model"]:
            return "capability_mismatch"
        selected = observed.get("selected_controls")
        supported = observed.get("supported_controls")
        if not isinstance(selected, dict) or not isinstance(supported, list):
            return "capability_mismatch"
        required = request["controls"]
        if not set(required).issubset(set(supported)) or selected != required:
            return "capability_mismatch"
        if not observed.get("evidence_ref"):
            return "missing_evidence"
        displayed_cost = observed.get("displayed_cost")
        if displayed_cost is None:
            return "uncertain_cost"
        if request["expected_cost"] is not None and displayed_cost != request["expected_cost"]:
            return "cost_mismatch"
        if (request.get("free_credits_only") or Decimal(displayed_cost["amount"]) == 0) and (
            observed.get("free_credit_confirmed") is not True
            or not observed.get("free_credit_evidence_ref")
        ):
            return "unconfirmed_free_credit"
        if request.get("free_credits_only") and Decimal(displayed_cost["amount"]) > 0:
            balance = observed.get("displayed_free_balance")
            if not isinstance(balance, dict) or balance.get("unit") != displayed_cost["unit"]:
                return "unconfirmed_free_credit"
            if Decimal(balance["amount"]) < Decimal(displayed_cost["amount"]):
                return "insufficient_free_credit"
        return "ready"


class KlingVisibleAdapter(_CalibratedVisibleAdapter):
    website = "kling"


class DreaminaVisibleAdapter(_CalibratedVisibleAdapter):
    website = "dreamina"


class PixVerseVisibleAdapter(_CalibratedVisibleAdapter):
    website = "pixverse"


class ViduVisibleAdapter(_CalibratedVisibleAdapter):
    website = "vidu"


class FlowVisibleAdapter(_CalibratedVisibleAdapter):
    website = "flow"


class KreaVisibleAdapter(_CalibratedVisibleAdapter):
    website = "krea"


class SeaArtVisibleAdapter(_CalibratedVisibleAdapter):
    website = "seaart"


class OpenArtVisibleAdapter(_CalibratedVisibleAdapter):
    website = "openart"


class HailuoVisibleAdapter(_CalibratedVisibleAdapter):
    website = "hailuo"


ADAPTERS: dict[str, WebProviderAdapter] = {
    adapter.website: adapter for adapter in (
        KlingVisibleAdapter(), DreaminaVisibleAdapter(), PixVerseVisibleAdapter(), ViduVisibleAdapter(),
        FlowVisibleAdapter(), KreaVisibleAdapter(), SeaArtVisibleAdapter(),
        OpenArtVisibleAdapter(), HailuoVisibleAdapter(),
    )
}


def evaluate_observation(website: str, request: Mapping[str, Any], observed: Mapping[str, Any]) -> str:
    return ADAPTERS[website].evaluate(request, observed)
