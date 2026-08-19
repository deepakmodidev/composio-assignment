"""Rendered-page verification for the fixed ten-app holdout.

This is deliberately separate from the applicant's CSV review. It opens the
recorded evidence URLs in Chromium, reads rendered body text, and checks that
one submitted passage for each positive field is visibly present.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from playwright.async_api import Browser, async_playwright

from research_agent.models import MAJOR_FIELDS, Evidence, ResearchRecord
from research_agent.pipeline import ARTIFACTS_DIR, HOLDOUT_IDS, append_log, write_json
from research_agent.sources import passage_is_on_page, utc_now


FINAL_PATH = ARTIFACTS_DIR / "final.json"
VERIFICATION_PATH = ARTIFACTS_DIR / "verification.json"


def load_holdout() -> list[ResearchRecord]:
    if not FINAL_PATH.exists():
        raise FileNotFoundError("artifacts/final.json is required")
    payload = json.loads(FINAL_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("artifacts/final.json must contain a record list")
    records = [ResearchRecord.model_validate(item) for item in payload]
    by_id = {record.id: record for record in records}
    if set(HOLDOUT_IDS) - set(by_id):
        raise ValueError("final.json does not contain the fixed holdout")
    return [by_id[app_id] for app_id in HOLDOUT_IDS]


def evidence_for_field(record: ResearchRecord, field: str) -> list[Evidence]:
    return [
        evidence
        for evidence in record.evidence
        if evidence.field == field and evidence.claim_supported
    ]


async def inspect_url(browser: Browser, url: str, timeout_ms: int) -> dict[str, Any]:
    page = await browser.new_page()
    try:
        # Some documentation sites keep loading analytics forever. A committed
        # response plus rendered body text is enough for this read-only check.
        response = await page.goto(url, wait_until="commit", timeout=timeout_ms)
        try:
            await page.wait_for_load_state(
                "domcontentloaded", timeout=min(timeout_ms, 8_000)
            )
        except Exception:
            pass
        await page.wait_for_timeout(750)
        body = await page.locator("body").inner_text(timeout=timeout_ms)
        status = response.status if response else 200
        return {
            "loaded": 200 <= status < 400 and len(body.strip()) >= 80,
            "body": body,
            "page_title": await page.title(),
            "final_url": page.url,
            "http_status": status,
            "error": "",
        }
    except Exception as exc:
        return {
            "loaded": False,
            "body": "",
            "page_title": "",
            "final_url": page.url,
            "http_status": -1,
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    finally:
        await page.close()


async def inspect_app(
    browser: Browser,
    record: ResearchRecord,
    timeout_ms: int,
) -> list[dict[str, Any]]:
    evidence_by_field: dict[str, list[Evidence]] = {
        field: evidence_for_field(record, field) for field in MAJOR_FIELDS
    }
    urls = sorted(
        {
            str(evidence.url)
            for candidates in evidence_by_field.values()
            for evidence in candidates
        }
    )
    pages = await asyncio.gather(
        *(inspect_url(browser, url, timeout_ms) for url in urls)
    )
    page_by_url = dict(zip(urls, pages, strict=True))
    rows: list[dict[str, Any]] = []
    for field in MAJOR_FIELDS:
        candidates = evidence_by_field[field]
        if not candidates:
            rows.append(
                {
                    "app_id": record.id,
                    "app_name": record.name,
                    "field": field,
                    "url": None,
                    "checked_at": utc_now(),
                    "browser_loaded": False,
                    "visible_support": "unresolved",
                    "notes": "Final record intentionally has no positive evidence for this field.",
                }
            )
            continue

        supported = next(
            (
                evidence
                for evidence in candidates
                if page_by_url[str(evidence.url)]["loaded"]
                and passage_is_on_page(
                    evidence.supporting_text,
                    page_by_url[str(evidence.url)]["body"],
                )
            ),
            None,
        )
        loaded_candidates = [
            (candidate, page_by_url[str(candidate.url)])
            for candidate in candidates
            if page_by_url[str(candidate.url)]["loaded"]
        ]
        if supported:
            evidence = supported
            page = page_by_url[str(evidence.url)]
            outcome = "supported"
            notes = "Rendered body contains a submitted normalized evidence passage."
        elif loaded_candidates:
            evidence, page = loaded_candidates[0]
            outcome = "not_visible"
            notes = (
                f"Browser rendered {len(loaded_candidates)} source page(s), but no submitted "
                "passage for this field was visible in their body text."
            )
        else:
            evidence = candidates[0]
            page = page_by_url[str(evidence.url)]
            outcome = "load_failed"
            notes = (
                f"Browser did not return usable rendered text from {len(candidates)} "
                f"candidate source(s). {page['error']}"
            ).strip()
        url = str(evidence.url)
        rows.append(
            {
                "app_id": record.id,
                "app_name": record.name,
                "field": field,
                "url": url,
                "checked_at": utc_now(),
                "browser_loaded": page["loaded"],
                "visible_support": outcome,
                "notes": notes,
                "page_title": page["page_title"],
                "final_url": page["final_url"],
                "http_status": page["http_status"],
            }
        )
    return rows


def save_rows(rows: list[dict[str, Any]]) -> None:
    if not VERIFICATION_PATH.exists():
        raise FileNotFoundError("artifacts/verification.json is required")
    payload = json.loads(VERIFICATION_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("verification.json must contain an object")
    payload["independent_browser_review"] = sorted(
        rows,
        key=lambda row: (int(row["app_id"]), MAJOR_FIELDS.index(row["field"])),
    )
    payload["browser_review_generated_at"] = utc_now()
    write_json(VERIFICATION_PATH, payload)


async def run(timeout_ms: int) -> None:
    records = load_holdout()
    all_rows: list[dict[str, Any]] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for record in records:
                rows = await inspect_app(browser, record, timeout_ms)
                all_rows.extend(rows)
                save_rows(all_rows)
                append_log(
                    phase="independent_browser_review",
                    status="complete",
                    app_id=record.id,
                    tool="chromium_rendered_page",
                )
                supported = sum(
                    row["visible_support"] == "supported" for row in rows
                )
                print(f"{record.id:03d} {record.name}: {supported}/7 rendered claims supported")
        finally:
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Browser-check the fixed ten-app, seven-field holdout"
    )
    parser.add_argument("--timeout-ms", type=int, default=20_000)
    args = parser.parse_args()
    asyncio.run(run(max(5_000, min(args.timeout_ms, 60_000))))


if __name__ == "__main__":
    main()
