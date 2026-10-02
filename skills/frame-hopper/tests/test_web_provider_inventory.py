"""The inventory must not turn advertised grants into account balances."""

from __future__ import annotations

import pytest

from backend.services.web_provider_inventory import (
    inventory_report, scan_visible_balances, visible_balance_candidates,
)
from tools.cli.web_provider_inventory import _catalog, main


def test_balance_parser_ignores_marketing_and_flags_ambiguous_values() -> None:
    assert visible_balance_candidates("Basic plan: 30 daily credits\n50 credits per day") == {
        "visible_free_credits": None,
        "visible_unspecified_credits": None,
        "balance_status": "not_found",
    }
    assert visible_balance_candidates("Free credits remaining: 42\nCredits: 60") == {
        "visible_free_credits": 42,
        "visible_unspecified_credits": 60,
        "balance_status": "observed_unverified",
    }
    assert visible_balance_candidates("Free credits remaining: 42\nFree credits remaining: 12")[
        "balance_status"] == "ambiguous"


def test_inventory_is_one_model_per_row_and_never_infers_generations() -> None:
    providers = _catalog("kling", None, False)
    report = inventory_report(providers, {"kling": {
        "account_status": "unverified", "balance_status": "observed_unverified",
        "visible_free_credits": 42,
    }}, model="kling")
    assert all(row["model"] == "kling" for row in report["rows"])
    assert len(report["rows"]) == len(providers)
    assert report["rows"][0]["credit_pool"] == "shared_by_site"
    assert report["rows"][0]["generations_possible"] is None
    assert report["rows"][0]["exact_model_cost"] is None


def test_live_scan_refuses_to_clear_a_staged_job() -> None:
    class Browser:
        def session(self):
            return {"active_provider": None, "staged_job_count": 1}

        def open(self, _site):
            raise AssertionError("scan must not navigate")

    with pytest.raises(RuntimeError, match="staged uploads"):
        scan_visible_balances(Browser(), _catalog("kling", None, True))


def test_cli_catalog_filters_models(capsys) -> None:
    assert main(["--model", "kling", "--free-only", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"site": "kling"' in output
    assert '"model": "kling"' in output
    assert '"site": "dreamina"' not in output


def test_audio_catalog_omits_video_models(capsys) -> None:
    assert main(["--media", "audio", "--free-only", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"model": "elevenlabs"' in output
    assert '"model": "ray"' not in output


def test_cli_help_shows_invocation_examples(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--help"])
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "usage: python -m tools.cli.web_provider_inventory" in help_text
    assert "python -m tools.cli.web_provider_inventory --live" in help_text
    assert "--model kling --free-only --live" in help_text
    assert "--media audio --live --json" in help_text
