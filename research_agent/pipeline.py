from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from composio import Composio
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import ValidationError

from research_agent.models import (
    MAJOR_FIELDS,
    AccessStatus,
    ApiBreadth,
    ApiSurface,
    AppInput,
    AuthMethod,
    Buildability,
    BuildabilityVerdict,
    ComposioCoverage,
    Confidence,
    ConfidenceMap,
    CredentialAccess,
    Evidence,
    ExtractedResearch,
    FetchedPage,
    McpStatus,
    McpSurface,
    RecordAudit,
    ResearchRecord,
    SourceDiscovery,
)
from research_agent.sources import (
    canonical_url,
    fetch_sources,
    hint_domains,
    passage_is_on_page,
    utc_now,
)


ROOT = Path(__file__).resolve().parents[1]
APPS_PATH = ROOT / "data" / "apps.json"
ARTIFACTS_DIR = ROOT / "artifacts"
APP_DIR = ARTIFACTS_DIR / "apps"
ATTEMPT_DIR = ARTIFACTS_DIR / "attempts"
SOURCE_CACHE_DIR = ARTIFACTS_DIR / "source_cache"
RUN_LOG_PATH = ARTIFACTS_DIR / "run_log.jsonl"
V1_PATH = ARTIFACTS_DIR / "v1.json"

PILOT_IDS = (1, 11, 21, 31, 41, 51, 61, 71, 81, 91)
HOLDOUT_IDS = (2, 12, 22, 32, 42, 52, 62, 72, 82, 92)
MAX_ATTEMPTS = 3


DISCOVERY_INSTRUCTIONS = """
You are the source-discovery stage of an evidence-first app research pipeline.

You MUST do both of these tool actions:
1. Call the Composio MCP tool COMPOSIO_SEARCH_TOOLS to determine whether the named
   app currently has a Composio toolkit and which representative tools are exposed.
2. Use web search to find the app vendor's current first-party pages for the requested
   fields. Search is discovery only; its snippets will never be treated as evidence.

Return at most 14 precise URLs. Prefer the assignment's official domain, exact auth/API
reference/onboarding/pricing pages, and the vendor's own MCP page or repository. A normal
claim may use only vendor-controlled sources. For a community MCP only, an original public
GitHub/GitLab repository is allowed and must be source_type=community_repository. Never use
directories, listicles, blogs, news, forum answers, or AI summaries. Do not infer that an
API or MCP does not exist. If nothing reliable is found, list that field as unresolved.

Composio catalog coverage is a separate signal from whether the app itself publishes an MCP.
Do not confuse those two concepts.
""".strip()


EXTRACTION_INSTRUCTIONS = """
You extract one app record using ONLY the fetched page text supplied by the caller.
Do not use memory, search snippets, or unstated knowledge.

Rules:
- Every non-unknown factual field needs one or more evidence items mapped to that field.
- supporting_text must be a short contiguous verbatim passage copied from the supplied page.
- Classify OAuth2 only when the page explicitly identifies OAuth 2.0; generic "OAuth" is
  insufficient for that normalized value.
- Use unknown when evidence is insufficient or contradictory.
- Use none_found only for an unsuccessful documented search; it is not proof of nonexistence.
- Description: one clear sentence supported by the official product/docs page.
- API breadth: broad=many resource families/workflows; moderate=several useful families;
  narrow=a small/specialized surface. Do not estimate breadth without an API index/reference.
- Buildability ready requires a useful API plus a self-serve credential path. conditional means
  payment, admin setup, unusual setup, or narrow scope. blocked requires affirmative official
  evidence of partnership/unavailable credentials/no usable public path. Missing docs => unknown.
- Community MCP evidence is allowed only from its original repository and official_source=false.
- For all other evidence, official_source must be true.
- Return all seven fixed confidence fields. Low confidence does not permit an unsupported claim;
  downgrade the value to unknown instead.
""".strip()


CRITIC_INSTRUCTIONS = """
You are an independent evidence critic. Use ONLY the supplied record and fetched page text.
For each of the seven fields, decide whether the value is directly supported by its mapped
evidence. Verify semantic support, not just word overlap. Derived buildability is supported only
when official evidence establishes the API, credential path, and stated blocker. Treat unknown
or none_found values as acceptable when the pipeline explicitly lacked evidence. A community MCP
may use its original repository; all other factual claims require first-party evidence. Return
exactly one audit entry for each field and list every unsupported field in failed_fields.
""".strip()


def ensure_directories() -> None:
    for path in (ARTIFACTS_DIR, APP_DIR, ATTEMPT_DIR, SOURCE_CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def append_log(
    *,
    phase: str,
    status: str,
    app_id: int | None = None,
    attempt: int | None = None,
    tool: str | None = None,
) -> None:
    ensure_directories()
    entry = {
        "timestamp": utc_now(),
        "app_id": app_id,
        "phase": phase,
        "attempt": attempt,
        "tool": tool,
        "status": status,
    }
    with RUN_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_apps() -> list[AppInput]:
    raw = json.loads(APPS_PATH.read_text(encoding="utf-8"))
    apps = [AppInput.model_validate(item) for item in raw]
    if len(apps) != 100 or len({app.id for app in apps}) != 100:
        raise ValueError("data/apps.json must contain exactly 100 unique apps")
    return apps


def record_path(app_id: int) -> Path:
    return APP_DIR / f"{app_id:03d}.json"


def load_record(app_id: int) -> ResearchRecord | None:
    path = record_path(app_id)
    if not path.exists():
        return None
    try:
        return ResearchRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, json.JSONDecodeError):
        return None


def save_record(record: ResearchRecord) -> None:
    write_json(record_path(record.id), record.model_dump(mode="json"))


def require_environment() -> tuple[str, str]:
    load_dotenv(ROOT / ".env")
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    composio_key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not openai_key or not composio_key:
        raise RuntimeError("OPENAI_API_KEY and COMPOSIO_API_KEY are required")
    return openai_key, composio_key


def create_composio_mcp_session(composio_key: str) -> Any:
    composio = Composio(api_key=composio_key)
    return composio.sessions.create(
        user_id="composio-assignment-research",
        mcp=True,
        sandbox={"enable": False},
    )


def output_item_types(response: Any) -> list[str]:
    return [str(getattr(item, "type", "unknown")) for item in response.output]


async def discover_sources(
    client: AsyncOpenAI,
    session: Any,
    app: AppInput,
    fields: Iterable[str],
    model: str,
) -> SourceDiscovery:
    headers = {
        key: value
        for key, value in (session.mcp.headers or {}).items()
        if value is not None
    }
    web_tool: dict[str, Any] = {"type": "web_search", "search_context_size": "medium"}
    domains = hint_domains(app.website_hint)
    if domains:
        web_tool["filters"] = {"allowed_domains": [*domains, "github.com"]}

    prompt = (
        f"{DISCOVERY_INSTRUCTIONS}\n\n"
        f"Current date: {datetime.now(UTC).date().isoformat()}\n"
        f"App: {app.name}\nCategory: {app.category}\n"
        f"Official website hint: {app.website_hint}\n"
        f"Fields needing research: {', '.join(fields)}"
    )
    response = await client.responses.parse(
        model=model,
        input=prompt,
        tools=[
            web_tool,
            {
                "type": "mcp",
                "server_label": "composio_catalog",
                "server_url": str(session.mcp.url),
                "headers": headers,
                "require_approval": "never",
            },
        ],
        text_format=SourceDiscovery,
    )
    tool_types = output_item_types(response)
    mcp_calls = [item for item in response.output if getattr(item, "type", "") == "mcp_call"]
    mcp_called = bool(mcp_calls)
    mcp_completed = any(
        getattr(item, "status", None) == "completed"
        and getattr(item, "error", None) is None
        for item in mcp_calls
    )
    web_called = "web_search_call" in tool_types
    append_log(
        phase="discovery",
        status="ok"
        if mcp_completed and web_called
        else "mcp_failed"
        if mcp_called and not mcp_completed
        else "missing_required_tool",
        app_id=app.id,
        tool="web_search+composio_mcp",
    )
    if not mcp_completed or not web_called:
        raise RuntimeError("Discovery did not complete both required tools successfully")
    if response.output_parsed is None:
        raise RuntimeError("Discovery returned no structured result")

    discovery = response.output_parsed
    coverage = discovery.composio_coverage.model_copy(update={"searched": True})
    return discovery.model_copy(update={"composio_coverage": coverage})


def pages_for_prompt(pages: list[FetchedPage]) -> str:
    compact_pages = []
    for page in pages:
        compact_pages.append(
            {
                "url": str(page.requested_url),
                "final_url": str(page.final_url),
                "page_title": page.page_title,
                "source_type": page.source_type,
                "official_source": page.official_source,
                "fetched_at": page.fetched_at,
                "http_status": page.http_status,
                "text": page.text[:12_000],
            }
        )
    return json.dumps(compact_pages, ensure_ascii=False)


async def extract_record(
    client: AsyncOpenAI,
    app: AppInput,
    pages: list[FetchedPage],
    fields: Iterable[str],
    model: str,
) -> ExtractedResearch:
    prompt = (
        f"{EXTRACTION_INSTRUCTIONS}\n\n"
        f"App metadata: {app.model_dump_json()}\n"
        f"Fields requested in this attempt: {', '.join(fields)}\n"
        f"Fetched pages:\n{pages_for_prompt(pages)}"
    )
    response = await client.responses.parse(
        model=model,
        input=prompt,
        text_format=ExtractedResearch,
    )
    if response.output_parsed is None:
        raise RuntimeError("Extraction returned no structured result")
    append_log(phase="extraction", status="ok", app_id=app.id, tool="openai")
    return reconcile_evidence(response.output_parsed, pages)


def reconcile_evidence(
    extracted: ExtractedResearch, pages: list[FetchedPage]
) -> ExtractedResearch:
    page_map: dict[str, FetchedPage] = {}
    for page in pages:
        page_map[canonical_url(str(page.requested_url))] = page
        page_map[canonical_url(str(page.final_url))] = page

    checked: list[Evidence] = []
    for evidence in extracted.evidence:
        page = page_map.get(canonical_url(str(evidence.url)))
        if page is None:
            continue
        supported = passage_is_on_page(evidence.supporting_text, page.text)
        checked.append(
            evidence.model_copy(
                update={
                    "url": page.requested_url,
                    "page_title": page.page_title,
                    "source_type": page.source_type,
                    "fetched_at": page.fetched_at,
                    "http_status": page.http_status,
                    "official_source": page.official_source,
                    "claim_supported": supported,
                }
            )
        )
    return extracted.model_copy(update={"evidence": checked})


def field_is_unknown(record: ExtractedResearch | ResearchRecord, field: str) -> bool:
    if field == "description":
        return record.description.lower().startswith("unknown")
    if field == "auth_methods":
        return not record.auth_methods or record.auth_methods == [AuthMethod.UNKNOWN]
    if field == "credential_access":
        return record.credential_access.status == AccessStatus.UNKNOWN
    if field == "api_styles":
        return not record.api_surface.styles or record.api_surface.styles == ["unknown"]
    if field == "api_breadth":
        return record.api_surface.breadth in {ApiBreadth.UNKNOWN, ApiBreadth.NONE_FOUND}
    if field == "mcp":
        return record.mcp.status in {McpStatus.UNKNOWN, McpStatus.NONE_FOUND}
    if field == "buildability":
        return record.buildability.verdict == BuildabilityVerdict.UNKNOWN
    raise ValueError(f"Unknown research field: {field}")


def deterministic_failed_fields(record: ExtractedResearch | ResearchRecord) -> list[str]:
    failed: list[str] = []
    for field in MAJOR_FIELDS:
        if field_is_unknown(record, field):
            continue
        mapped = [
            evidence
            for evidence in record.evidence
            if evidence.field == field and evidence.claim_supported and evidence.http_status == 200
        ]
        if field == "mcp" and record.mcp.status == McpStatus.COMMUNITY:
            valid = any(
                evidence.source_type == "community_repository"
                and not evidence.official_source
                for evidence in mapped
            )
        else:
            valid = any(evidence.official_source for evidence in mapped)
        if not valid:
            failed.append(field)
    return failed


async def independent_audit(
    client: AsyncOpenAI,
    record: ResearchRecord,
    pages: list[FetchedPage],
    model: str,
) -> RecordAudit:
    prompt = (
        f"{CRITIC_INSTRUCTIONS}\n\n"
        f"Record:\n{record.model_dump_json()}\n\n"
        f"Fetched pages:\n{pages_for_prompt(pages)}"
    )
    response = await client.responses.parse(
        model=model,
        input=prompt,
        text_format=RecordAudit,
    )
    if response.output_parsed is None:
        raise RuntimeError("Independent critic returned no structured result")
    audit = response.output_parsed
    seen = {item.field for item in audit.fields}
    missing = set(MAJOR_FIELDS) - seen
    failed = set(audit.failed_fields) | missing | set(deterministic_failed_fields(record))
    for item in audit.fields:
        if not item.supported and not field_is_unknown(record, item.field):
            failed.add(item.field)
    normalized = audit.model_copy(
        update={
            "failed_fields": sorted(failed),
            "all_confident_claims_supported": not failed,
        }
    )
    append_log(
        phase="independent_audit",
        status="ok" if not failed else "failed_fields",
        app_id=record.id,
        tool="openai_fresh_context",
    )
    return normalized


def unknown_extracted(extra_issues: list[str] | None = None) -> ExtractedResearch:
    low = Confidence.LOW
    return ExtractedResearch(
        description="Unknown — official evidence was insufficient.",
        auth_methods=[AuthMethod.UNKNOWN],
        credential_access=CredentialAccess(
            status=AccessStatus.UNKNOWN,
            requirements="Official evidence was insufficient.",
        ),
        api_surface=ApiSurface(
            styles=[], breadth=ApiBreadth.UNKNOWN, notes="Official evidence was insufficient."
        ),
        mcp=McpSurface(
            status=McpStatus.UNKNOWN,
            url=None,
            notes="No supported conclusion from the fetched sources.",
        ),
        buildability=Buildability(
            verdict=BuildabilityVerdict.UNKNOWN,
            reason="Official evidence was insufficient.",
            main_blocker="Unknown; no blocker was inferred.",
        ),
        confidence=ConfidenceMap(
            description=low,
            auth_methods=low,
            credential_access=low,
            api_styles=low,
            api_breadth=low,
            mcp=low,
            buildability=low,
        ),
        evidence=[],
        unresolved_issues=extra_issues or ["No fetchable first-party evidence was found."],
    )


def downgrade_fields(
    extracted: ExtractedResearch, failed_fields: Iterable[str]
) -> ExtractedResearch:
    failed = set(failed_fields)
    data = extracted.model_dump(mode="python")
    confidence = extracted.confidence.model_dump(mode="python")
    for field in failed:
        confidence[field] = Confidence.LOW
    data["confidence"] = ConfidenceMap.model_validate(confidence)
    data["evidence"] = [item for item in extracted.evidence if item.field not in failed]

    if "description" in failed:
        data["description"] = "Unknown — official evidence was insufficient."
    if "auth_methods" in failed:
        data["auth_methods"] = [AuthMethod.UNKNOWN]
    if "credential_access" in failed:
        data["credential_access"] = CredentialAccess(
            status=AccessStatus.UNKNOWN, requirements="Official evidence was insufficient."
        )
    surface = extracted.api_surface.model_copy()
    if "api_styles" in failed:
        surface = surface.model_copy(update={"styles": []})
    if "api_breadth" in failed:
        surface = surface.model_copy(update={"breadth": ApiBreadth.UNKNOWN})
    data["api_surface"] = surface
    if "mcp" in failed:
        data["mcp"] = McpSurface(
            status=McpStatus.UNKNOWN,
            url=None,
            notes="No supported conclusion from the fetched sources.",
        )
    if "buildability" in failed:
        data["buildability"] = Buildability(
            verdict=BuildabilityVerdict.UNKNOWN,
            reason="Official evidence was insufficient.",
            main_blocker="Unknown; no blocker was inferred.",
        )
    issues = [*extracted.unresolved_issues]
    issues.extend(f"Unsupported after bounded verification: {field}" for field in sorted(failed))
    data["unresolved_issues"] = list(dict.fromkeys(issues))
    return ExtractedResearch.model_validate(data)


def merge_extracted_fields(
    base: ExtractedResearch,
    update: ExtractedResearch,
    fields: Iterable[str],
) -> ExtractedResearch:
    fields = set(fields)
    values = base.model_dump(mode="python")
    confidence = base.confidence.model_dump(mode="python")
    update_confidence = update.confidence.model_dump(mode="python")
    if "description" in fields:
        values["description"] = update.description
    if "auth_methods" in fields:
        values["auth_methods"] = update.auth_methods
    if "credential_access" in fields:
        values["credential_access"] = update.credential_access
    surface = base.api_surface.model_copy()
    if "api_styles" in fields:
        surface = surface.model_copy(update={"styles": update.api_surface.styles})
    if "api_breadth" in fields:
        surface = surface.model_copy(
            update={"breadth": update.api_surface.breadth, "notes": update.api_surface.notes}
        )
    values["api_surface"] = surface
    if "mcp" in fields:
        values["mcp"] = update.mcp
    if "buildability" in fields:
        values["buildability"] = update.buildability
    for field in fields:
        confidence[field] = update_confidence[field]
    values["confidence"] = confidence
    values["evidence"] = [item for item in base.evidence if item.field not in fields] + [
        item for item in update.evidence if item.field in fields
    ]
    values["unresolved_issues"] = list(
        dict.fromkeys([*base.unresolved_issues, *update.unresolved_issues])
    )
    return ExtractedResearch.model_validate(values)


def combine_record(
    app: AppInput,
    extracted: ExtractedResearch,
    coverage: ComposioCoverage,
    attempt: int,
    status: str,
) -> ResearchRecord:
    return ResearchRecord(
        **app.model_dump(),
        **extracted.model_dump(),
        composio_coverage=coverage,
        attempt_count=attempt,
        status=status,
    )


async def research_app(
    client: AsyncOpenAI,
    session: Any,
    app: AppInput,
    research_model: str,
    verify_model: str,
    *,
    requested_fields: Iterable[str] = MAJOR_FIELDS,
    force: bool = False,
    max_attempts: int = MAX_ATTEMPTS,
    save_result: bool = True,
) -> ResearchRecord:
    if not force:
        existing = load_record(app.id)
        if existing is not None:
            append_log(phase="research", status="resumed", app_id=app.id)
            return existing

    fields = list(requested_fields)
    last_extracted: ExtractedResearch | None = None
    last_coverage = ComposioCoverage(
        searched=False, toolkit_found=False, relevant_tools=[], notes="Search not completed."
    )
    accumulated_issues: list[str] = []
    accumulated_pages: dict[str, FetchedPage] = {}

    for attempt in range(1, min(max_attempts, MAX_ATTEMPTS) + 1):
        append_log(phase="research", status="started", app_id=app.id, attempt=attempt)
        try:
            discovery = await discover_sources(client, session, app, fields, research_model)
            last_coverage = discovery.composio_coverage
            pages, fetch_issues = await fetch_sources(app, discovery.official_sources)
            accumulated_issues.extend(fetch_issues)
            write_json(
                SOURCE_CACHE_DIR / f"{app.id:03d}-attempt-{attempt}.json",
                [page.model_dump(mode="json") for page in pages],
            )
            if not pages:
                raise RuntimeError("No accepted first-party pages could be fetched")
            for page in pages:
                accumulated_pages[canonical_url(str(page.requested_url))] = page
            fresh_extracted = await extract_record(client, app, pages, fields, research_model)
            extracted = (
                merge_extracted_fields(last_extracted, fresh_extracted, fields)
                if last_extracted is not None
                else fresh_extracted
            )
            extracted = extracted.model_copy(
                update={
                    "unresolved_issues": list(
                        dict.fromkeys(
                            [
                                *extracted.unresolved_issues,
                                *discovery.unresolved_fields,
                                *fetch_issues,
                            ]
                        )
                    )
                }
            )
            all_pages = list(accumulated_pages.values())
            extracted = reconcile_evidence(extracted, all_pages)
            last_extracted = extracted
            candidate = combine_record(
                app, extracted, last_coverage, attempt, status="complete"
            )
            audit = await independent_audit(client, candidate, all_pages, verify_model)
            write_json(
                ATTEMPT_DIR / f"{app.id:03d}-attempt-{attempt}.json",
                {
                    "record": candidate.model_dump(mode="json"),
                    "audit": audit.model_dump(mode="json"),
                },
            )
            if audit.all_confident_claims_supported:
                if save_result:
                    save_record(candidate)
                append_log(
                    phase="research", status="complete", app_id=app.id, attempt=attempt
                )
                return candidate
            fields = audit.failed_fields
        except Exception as error:
            accumulated_issues.append(
                f"Attempt {attempt} failed during {type(error).__name__}."
            )
            append_log(
                phase="research",
                status=f"error:{type(error).__name__}",
                app_id=app.id,
                attempt=attempt,
            )

        if attempt < max_attempts:
            await asyncio.sleep(min(2**attempt, 4))

    if last_extracted is None:
        final_extracted = unknown_extracted(list(dict.fromkeys(accumulated_issues)))
    else:
        final_extracted = downgrade_fields(last_extracted, fields)
        final_extracted = final_extracted.model_copy(
            update={
                "unresolved_issues": list(
                    dict.fromkeys([*final_extracted.unresolved_issues, *accumulated_issues])
                )
            }
        )
    partial = combine_record(
        app, final_extracted, last_coverage, min(max_attempts, MAX_ATTEMPTS), status="partial"
    )
    if save_result:
        save_record(partial)
    append_log(phase="research", status="partial", app_id=app.id, attempt=max_attempts)
    return partial


def freeze_v1() -> None:
    apps = load_apps()
    records = [load_record(app.id) for app in apps]
    missing = [app.id for app, record in zip(apps, records, strict=True) if record is None]
    if missing:
        raise RuntimeError(f"Cannot freeze V1; missing valid records for IDs: {missing}")
    payload = [record.model_dump(mode="json") for record in records if record is not None]
    if V1_PATH.exists():
        existing = json.loads(V1_PATH.read_text(encoding="utf-8"))
        if existing != payload:
            raise RuntimeError("artifacts/v1.json is already frozen and will not be overwritten")
        append_log(phase="freeze_v1", status="already_frozen")
        return
    write_json(V1_PATH, payload)
    append_log(phase="freeze_v1", status="complete")


def select_apps(args: argparse.Namespace) -> list[AppInput]:
    apps = load_apps()
    if args.pilot:
        selected = set(PILOT_IDS)
    elif args.remaining:
        selected = set(range(1, 101)) - set(PILOT_IDS)
    elif args.app is not None:
        selected = {args.app}
    else:
        raise ValueError("Choose --pilot, --remaining, --app ID, or --freeze-v1")
    result = [app for app in apps if app.id in selected]
    if len(result) != len(selected):
        raise ValueError("One or more selected app IDs are invalid")
    return result


async def run_research(args: argparse.Namespace) -> None:
    openai_key, composio_key = require_environment()
    ensure_directories()
    apps = select_apps(args)
    research_model = os.getenv("RESEARCH_MODEL", "gpt-5.6-terra")
    verify_model = os.getenv("VERIFY_MODEL", "gpt-5.6-sol")
    client = AsyncOpenAI(api_key=openai_key, max_retries=2, timeout=180.0)
    session = await asyncio.to_thread(create_composio_mcp_session, composio_key)
    semaphore = asyncio.Semaphore(min(max(args.concurrency, 1), 5))

    async def run_one(app: AppInput) -> ResearchRecord:
        async with semaphore:
            return await research_app(
                client,
                session,
                app,
                research_model,
                verify_model,
                force=args.force,
            )

    await asyncio.gather(*(run_one(app) for app in apps))
    await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research the Composio 100-app set")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--pilot", action="store_true", help="Research the fixed pilot")
    selection.add_argument(
        "--remaining", action="store_true", help="Research the 90 non-pilot apps"
    )
    selection.add_argument("--app", type=int, help="Research one app ID")
    selection.add_argument(
        "--freeze-v1", action="store_true", help="Freeze all app records into V1"
    )
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.freeze_v1:
        ensure_directories()
        freeze_v1()
        return
    asyncio.run(run_research(args))


if __name__ == "__main__":
    main()
