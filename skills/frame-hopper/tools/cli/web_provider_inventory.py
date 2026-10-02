"""List model sites and optionally inspect visible free-credit balances."""

from __future__ import annotations

import argparse
import json
import sys

from backend.services.web_provider_browser_client import WebProviderBrowserClient
from backend.services.web_provider_catalog import select_providers
from backend.services.web_provider_inventory import inventory_report, scan_visible_balances


def _catalog(model: str | None, media: str | None, free_only: bool) -> list[dict]:
    return [{
        "name": item.name, "url": item.url, "media": list(item.media),
        "model_access": item.models, "free_offer": item.free_offer,
        "free_cadence": item.cadence, "source": item.source,
        "limitation": item.limitation,
    } for item in select_providers(model=model, media=media, free_only=free_only)]


def _print_table(report: dict, *, live: bool) -> None:
    current_site = None
    for row in report["rows"]:
        if row["site"] != current_site:
            current_site = row["site"]
            print(f"\n{current_site} | advertised: {row['advertised_offer']}")
        balance = row["visible_free_credits"]
        if balance is not None:
            visible = f"{balance} observed (unverified)"
        elif row["visible_unspecified_credits"] is not None:
            visible = f"{row['visible_unspecified_credits']} total observed (not identified as free)"
        else:
            visible = "unknown"
        print(f"  {row['model']}: {row['model_free_access']} access | "
              f"current free credits: {visible} | account: {row['account_status']}")
    print("\nCredits are shared by site, not allocated per model. Observed numbers are not verified entitlement.")
    print("Generation counts require the signed-in account's exact cost for the chosen model and settings.")
    if not live:
        print("Run with --live to inspect the API Chrome profile; this navigates its one active tab.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.cli.web_provider_inventory",
        description="List each site's model families and advertised free offer; optionally inspect visible account balances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Run from the project root or installed frame-hopper skill directory:
  python -m tools.cli.web_provider_inventory --help
  python -m tools.cli.web_provider_inventory              # offline catalog
  python -m tools.cli.web_provider_inventory --live       # scan all sites
  python -m tools.cli.web_provider_inventory --model kling --free-only --live
  python -m tools.cli.web_provider_inventory --media audio --live --json

The live scan uses the API's separate Chrome profile and navigates its one tab.
It will not disturb staged uploads. Visible numbers are unverified; usable
generations need a signed-in account and the exact selected model cost.""",
    )
    parser.add_argument("--model", metavar="FAMILY",
                        help="Only sites listing this model family (for example, kling)")
    parser.add_argument("--media", choices=("video", "audio"),
                        help="Only video or audio model rows")
    parser.add_argument("--free-only", action="store_true",
                        help="Include only confirmed or conditional free model access")
    parser.add_argument("--live", action="store_true",
                        help="Visit each selected site in the shared API Chrome tab and inspect visible balances")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        providers = _catalog(args.model, args.media, args.free_only)
    except ValueError as error:
        parser.error(str(error))
    observations = None
    if args.live:
        browser = WebProviderBrowserClient()
        browser.ensure_running()
        try:
            observations = scan_visible_balances(
                browser, providers,
                on_site=lambda name: print(f"Checking {name}...", file=sys.stderr)
                if not args.json else None,
            )
        except RuntimeError as error:
            parser.error(str(error))
    report = inventory_report(providers, observations, model=args.model,
                              media=args.media, free_only=args.free_only)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_table(report, live=args.live)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
