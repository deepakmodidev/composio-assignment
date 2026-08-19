from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from openai import AsyncOpenAI

from research_agent.models import (
    MAJOR_FIELDS,
    AppInput,
    DiscoveredSource,
    ExtractedResearch,
    RecordAudit,
    ResearchRecord,
)
from research_agent.pipeline import (
    ARTIFACTS_DIR,
    HOLDOUT_IDS,
    MAX_ATTEMPTS,
    PILOT_IDS,
    V1_PATH,
    append_log,
    combine_record,
    create_composio_mcp_session,
    deterministic_failed_fields,
    downgrade_fields,
    ensure_directories,
    independent_audit,
    load_apps,
    load_record,
    reconcile_evidence,
    research_app,
    require_environment,
    save_record,
    write_json,
)
from research_agent.sources import canonical_url, fetch_sources, passage_is_on_page, utc_now


FINAL_PATH = ARTIFACTS_DIR / "final.json"
VERIFICATION_PATH = ARTIFACTS_DIR / "verification.json"
PILOT_VERIFICATION_PATH = ARTIFACTS_DIR / "verification_pilot.json"


def as_extracted(record: ResearchRecord) -> ExtractedResearch:
    return ExtractedResearch(
        description=record.description,
        auth_methods=record.auth_methods,
        credential_access=record.credential_access,
        api_surface=record.api_surface,
        mcp=record.mcp,
        buildability=record.buildability,
        confidence=record.confidence,
        evidence=record.evidence,
        unresolved_issues=record.unresolved_issues,
    )


def load_frozen_v1() -> list[ResearchRecord]:
    if not V1_PATH.exists():
        raise FileNotFoundError("artifacts/v1.json does not exist; run --freeze-v1 first")
    payload = json.loads(V1_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("artifacts/v1.json must be a JSON list")
    records = [ResearchRecord.model_validate(item) for item in payload]
    if len(records) != 100 or {record.id for record in records} != set(range(1, 101)):
        raise ValueError("artifacts/v1.json must contain exactly 100 unique records")
    return sorted(records, key=lambda record: record.id)


def evidence_sources(record: ResearchRecord) -> list[DiscoveredSource]:
    sources: list[DiscoveredSource] = []
    seen: set[tuple[str, str]] = set()
    for evidence in record.evidence:
        key = (canonical_url(str(evidence.url)), evidence.source_type)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            DiscoveredSource(
                field=evidence.field,
                url=str(evidence.url),
                source_type=evidence.source_type,
                rationale="Re-fetch a submitted evidence URL.",
            )
        )
    return sources


async def refetch_and_check(
    app: AppInput, record: ResearchRecord
) -> tuple[ResearchRecord, list[Any], list[dict[str, Any]], list[str]]:
    pages, issues = await fetch_sources(app, evidence_sources(record))
    checked = reconcile_evidence(as_extracted(record), pages)
    status = "complete" if not deterministic_failed_fields(checked) else "partial"
    refreshed = combine_record(
        app,
        checked,
        record.composio_coverage,
        record.attempt_count,
        status=status,
    )
    page_map = {}
    for page in pages:
        page_map[canonical_url(str(page.requested_url))] = page
        page_map[canonical_url(str(page.final_url))] = page

    results: list[dict[str, Any]] = []
    for evidence in record.evidence:
        page = page_map.get(canonical_url(str(evidence.url)))
        passage_found = bool(
            page and passage_is_on_page(evidence.supporting_text, page.text)
        )
        results.append(
            {
                "app_id": record.id,
                "field": evidence.field,
                "url": str(evidence.url),
                "final_url": str(page.final_url) if page else None,
                "http_status": page.http_status if page else -1,
                "resolved": page is not None,
                "method": page.method if page else "failed",
                "official_source": page.official_source if page else evidence.official_source,
                "passage_found": passage_found,
                "claim_supported": passage_found and evidence.claim_supported,
                "checked_at": utc_now(),
            }
        )
    return refreshed, pages, results, issues


def merge_fields(
    base: ResearchRecord,
    correction: ResearchRecord,
    fields: Iterable[str],
) -> ResearchRecord:
    fields = set(fields)
    extracted = as_extracted(base)
    values = extracted.model_dump(mode="python")
    confidence = extracted.confidence.model_dump(mode="python")
    corrected_confidence = correction.confidence.model_dump(mode="python")

    if "description" in fields:
        values["description"] = correction.description
    if "auth_methods" in fields:
        values["auth_methods"] = correction.auth_methods
    if "credential_access" in fields:
        values["credential_access"] = correction.credential_access
    surface = extracted.api_surface.model_copy()
    if "api_styles" in fields:
        surface = surface.model_copy(update={"styles": correction.api_surface.styles})
    if "api_breadth" in fields:
        surface = surface.model_copy(
            update={
                "breadth": correction.api_surface.breadth,
                "notes": correction.api_surface.notes,
            }
        )
    values["api_surface"] = surface
    if "mcp" in fields:
        values["mcp"] = correction.mcp
    if "buildability" in fields:
        values["buildability"] = correction.buildability
    for field in fields:
        confidence[field] = corrected_confidence[field]
    values["confidence"] = confidence
    values["evidence"] = [item for item in base.evidence if item.field not in fields] + [
        item for item in correction.evidence if item.field in fields
    ]
    values["unresolved_issues"] = list(
        dict.fromkeys([*base.unresolved_issues, *correction.unresolved_issues])
    )
    merged = ExtractedResearch.model_validate(values)
    coverage = (
        correction.composio_coverage
        if correction.composio_coverage.searched
        else base.composio_coverage
    )
    return combine_record(
        AppInput(
            id=base.id,
            name=base.name,
            category=base.category,
            website_hint=base.website_hint,
        ),
        merged,
        coverage,
        max(base.attempt_count, correction.attempt_count),
        status="complete",
    )


def changed_fields(before: ResearchRecord, after: ResearchRecord) -> list[str]:
    before_data = as_extracted(before).model_dump(mode="json")
    after_data = as_extracted(after).model_dump(mode="json")
    mappings = {
        "description": lambda data: data["description"],
        "auth_methods": lambda data: data["auth_methods"],
        "credential_access": lambda data: data["credential_access"],
        "api_styles": lambda data: data["api_surface"]["styles"],
        "api_breadth": lambda data: data["api_surface"]["breadth"],
        "mcp": lambda data: data["mcp"],
        "buildability": lambda data: data["buildability"],
    }
    return [field for field, getter in mappings.items() if getter(before_data) != getter(after_data)]


async def verify_one(
    client: AsyncOpenAI,
    session: Any,
    app: AppInput,
    v1: ResearchRecord,
    research_model: str,
    verify_model: str,
    *,
    correct: bool,
    max_attempts: int,
) -> tuple[ResearchRecord, dict[str, Any], list[dict[str, Any]]]:
    refreshed, pages, url_results, fetch_issues = await refetch_and_check(app, v1)
    audit = await independent_audit(client, refreshed, pages, verify_model)
    initial_failed = audit.failed_fields
    final_record = refreshed
    all_url_results = list(url_results)

    if correct and initial_failed:
        correction = await research_app(
            client,
            session,
            app,
            research_model,
            verify_model,
            requested_fields=initial_failed,
            force=True,
            max_attempts=max_attempts,
            save_result=False,
        )
        merged = merge_fields(v1, correction, initial_failed)
        final_record, pages, corrected_results, correction_issues = await refetch_and_check(
            app, merged
        )
        all_url_results.extend(corrected_results)
        fetch_issues.extend(correction_issues)
        audit = await independent_audit(client, final_record, pages, verify_model)

    if audit.failed_fields:
        downgraded = downgrade_fields(as_extracted(final_record), audit.failed_fields)
        final_record = combine_record(
            app,
            downgraded,
            final_record.composio_coverage,
            final_record.attempt_count,
            status="partial",
        )
    else:
        final_record = final_record.model_copy(update={"status": "complete"})

    final_record, final_pages, final_url_results, final_issues = await refetch_and_check(
        app, final_record
    )
    all_url_results.extend(final_url_results)
    fetch_issues.extend(final_issues)
    final_audit = await independent_audit(client, final_record, final_pages, verify_model)
    if final_audit.failed_fields:
        downgraded = downgrade_fields(as_extracted(final_record), final_audit.failed_fields)
        final_record = combine_record(
            app,
            downgraded,
            final_record.composio_coverage,
            final_record.attempt_count,
            status="partial",
        )

    summary = {
        "app_id": app.id,
        "app_name": app.name,
        "initial_failed_fields": initial_failed,
        "final_failed_fields": deterministic_failed_fields(final_record),
        "changed_fields": changed_fields(v1, final_record),
        "fetch_issues": list(dict.fromkeys(fetch_issues)),
        "independent_agent_audit": final_audit.model_dump(mode="json"),
    }
    append_log(
        phase="verification",
        status="complete" if not summary["final_failed_fields"] else "partial",
        app_id=app.id,
        tool="url_refetch+independent_agent",
    )
    return final_record, summary, all_url_results


async def run(args: argparse.Namespace) -> None:
    openai_key, composio_key = require_environment()
    ensure_directories()
    apps = load_apps()
    app_by_id = {app.id: app for app in apps}
    research_model = os.getenv("RESEARCH_MODEL", "gpt-5.6-terra")
    verify_model = os.getenv("VERIFY_MODEL", "gpt-5.6-sol")
    client = AsyncOpenAI(api_key=openai_key, max_retries=2, timeout=180.0)
    session = await asyncio.to_thread(create_composio_mcp_session, composio_key)

    if args.pilot:
        source_records = []
        for app_id in PILOT_IDS:
            record = load_record(app_id)
            if record is None:
                raise FileNotFoundError(f"Missing pilot record for app {app_id}")
            source_records.append(record)
        selected_ids = set(PILOT_IDS)
        correct = False
        destination = PILOT_VERIFICATION_PATH
    else:
        source_records = load_frozen_v1()
        selected_ids = set(range(1, 101))
        correct = True
        destination = VERIFICATION_PATH

    semaphore = asyncio.Semaphore(min(max(args.concurrency, 1), 5))

    async def one(record: ResearchRecord):
        async with semaphore:
            return await verify_one(
                client,
                session,
                app_by_id[record.id],
                record,
                research_model,
                verify_model,
                correct=correct,
                max_attempts=args.max_attempts,
            )

    outputs = await asyncio.gather(
        *(one(record) for record in source_records if record.id in selected_ids)
    )
    final_records = sorted((item[0] for item in outputs), key=lambda record: record.id)
    app_audits = [item[1] for item in outputs]
    url_results = [result for item in outputs for result in item[2]]
    payload = {
        "generated_at": utc_now(),
        "scope": "pilot" if args.pilot else "all",
        "results": url_results,
        "app_audits": app_audits,
        "independent_browser_review": [],
    }
    write_json(destination, payload)
    if args.pilot:
        for record in final_records:
            save_record(record)
    else:
        write_json(FINAL_PATH, [record.model_dump(mode="json") for record in final_records])
    await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Re-fetch and independently verify research")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--pilot", action="store_true")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS, choices=(1, 2, 3))
    parser.add_argument("--concurrency", type=int, default=3)
    return parser


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
