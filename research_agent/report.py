"""Create the human audit sheet, computed metrics, and static case-study page.

This module deliberately has no network calls.  It only summarizes the frozen
artifacts produced by the pipeline and verification stages, so a report can be
regenerated without changing a research finding.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
SITE_DIR = PROJECT_ROOT / "site"
MANIFEST_PATH = PROJECT_ROOT / "data" / "apps.json"
V1_PATH = ARTIFACTS_DIR / "v1.json"
FINAL_PATH = ARTIFACTS_DIR / "final.json"
VERIFICATION_PATH = ARTIFACTS_DIR / "verification.json"
METRICS_PATH = ARTIFACTS_DIR / "metrics.json"
REVIEW_PATH = ARTIFACTS_DIR / "human_review.csv"

HOLDOUT_IDS = (2, 12, 22, 32, 42, 52, 62, 72, 82, 92)
REVIEW_FIELDS = (
    "description",
    "auth_methods",
    "credential_access",
    "api_styles",
    "api_breadth",
    "mcp",
    "buildability",
)
REVIEW_COLUMNS = (
    "app_id",
    "app_name",
    "field",
    "v1_value",
    "final_value",
    "official_evidence_url",
    "human_v1_result",
    "human_final_result",
    "human_correction",
    "human_notes",
)
VALID_HUMAN_RESULTS = {"correct", "incorrect", "unclear"}
SELF_SERVE_ACCESS = {"free", "free_trial"}
GATED_ACCESS = {"paid", "admin_gated", "partner_gated"}


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _records_from_payload(payload: Any, path: Path) -> list[dict[str, Any]]:
    """Accept the two small artifact shapes used by pipeline checkpoints."""
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("records", payload.get("apps"))
    else:
        records = None
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ValueError(f"{path} must contain a record list or a {{'records': [...]}} object")
    records = sorted(records, key=lambda record: int(record.get("id", 0)))
    ids = [record.get("id") for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path} contains duplicate app IDs")
    return records


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Required artifact is missing: {path}")
    return _records_from_payload(_read_json(path), path)


def load_manifest() -> list[dict[str, Any]]:
    manifest = _records_from_payload(_read_json(MANIFEST_PATH), MANIFEST_PATH)
    if len(manifest) != 100 or {row["id"] for row in manifest} != set(range(1, 101)):
        raise ValueError("data/apps.json must contain exactly the 100 assignment app IDs")
    return manifest


def _require_full_research_set(records: list[dict[str, Any]], label: str) -> None:
    ids = {record.get("id") for record in records}
    if len(records) != 100 or ids != set(range(1, 101)):
        raise ValueError(f"{label} must contain exactly 100 unique app records")


def _nested(record: dict[str, Any], *keys: str, default: Any = "") -> Any:
    value: Any = record
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key, default)
    return value


def _join(values: Any) -> str:
    if isinstance(values, list):
        return ", ".join(str(value) for value in values) or "unknown"
    return str(values or "unknown")


def field_value(record: dict[str, Any], field: str) -> str:
    """Return a stable, human-readable value for one of the seven audit fields."""
    if field == "description":
        return str(record.get("description") or "unknown")
    if field == "auth_methods":
        return _join(record.get("auth_methods"))
    if field == "credential_access":
        access = _nested(record, "credential_access", default={})
        if not isinstance(access, dict):
            return "unknown"
        status = str(access.get("status") or "unknown")
        requirements = str(access.get("requirements") or "")
        return f"{status} — {requirements}" if requirements else status
    if field == "api_styles":
        return _join(_nested(record, "api_surface", "styles", default=[]))
    if field == "api_breadth":
        return str(_nested(record, "api_surface", "breadth", default="unknown") or "unknown")
    if field == "mcp":
        mcp = _nested(record, "mcp", default={})
        if not isinstance(mcp, dict):
            return "unknown"
        status = str(mcp.get("status") or "unknown")
        notes = str(mcp.get("notes") or "")
        return f"{status} — {notes}" if notes else status
    if field == "buildability":
        buildability = _nested(record, "buildability", default={})
        if not isinstance(buildability, dict):
            return "unknown"
        verdict = str(buildability.get("verdict") or "unknown")
        blocker = str(buildability.get("main_blocker") or "")
        return f"{verdict} — {blocker}" if blocker else verdict
    raise ValueError(f"Unknown review field: {field}")


def evidence_url(record: dict[str, Any], field: str) -> str:
    evidence = record.get("evidence", [])
    if not isinstance(evidence, list):
        return ""
    for item in evidence:
        if isinstance(item, dict) and item.get("field") == field and item.get("official_source"):
            return str(item.get("url") or "")
    if field == "mcp":
        for item in evidence:
            if (
                isinstance(item, dict)
                and item.get("field") == "mcp"
                and item.get("source_type") == "community_repository"
            ):
                return str(item.get("url") or "")
    return ""


def review_source_url(
    final_record: dict[str, Any],
    v1_record: dict[str, Any],
    field: str,
) -> str:
    """Return a verified official starting page for a human field check.

    An unresolved field has no supporting citation by definition. In that case
    the sheet links to the closest already-fetched official page so the
    applicant can decide whether the unknown is warranted.
    """
    direct = evidence_url(final_record, field) or evidence_url(v1_record, field)
    if direct:
        return direct
    related_fields = {
        "api_breadth": ("api_styles", "description"),
        "api_styles": ("api_breadth", "description"),
        "credential_access": ("auth_methods", "buildability"),
        "buildability": ("credential_access", "api_breadth"),
    }.get(field, tuple(item for item in REVIEW_FIELDS if item != field))
    for related in related_fields:
        candidate = evidence_url(final_record, related) or evidence_url(
            v1_record, related
        )
        if candidate:
            return candidate
    return ""


def _existing_review_results() -> dict[tuple[str, str], dict[str, str]]:
    if not REVIEW_PATH.exists():
        return {}
    with REVIEW_PATH.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != REVIEW_COLUMNS:
            raise ValueError(f"{REVIEW_PATH} has an unexpected header")
        return {
            (row.get("app_id", ""), row.get("field", "")): {
                key: row.get(key, "")
                for key in ("human_v1_result", "human_final_result", "human_correction", "human_notes")
            }
            for row in reader
        }


def build_review_rows(v1_records: list[dict[str, Any]], final_records: list[dict[str, Any]]) -> list[dict[str, str]]:
    v1_by_id = {int(record["id"]): record for record in v1_records}
    final_by_id = {int(record["id"]): record for record in final_records}
    existing = _existing_review_results()
    rows: list[dict[str, str]] = []
    for app_id in HOLDOUT_IDS:
        if app_id not in v1_by_id or app_id not in final_by_id:
            raise ValueError(f"Both V1 and final records are required for holdout app {app_id}")
        v1_record, final_record = v1_by_id[app_id], final_by_id[app_id]
        for field in REVIEW_FIELDS:
            prior = existing.get((str(app_id), field), {})
            rows.append(
                {
                    "app_id": str(app_id),
                    "app_name": str(final_record.get("name") or v1_record.get("name") or ""),
                    "field": field,
                    "v1_value": field_value(v1_record, field),
                    "final_value": field_value(final_record, field),
                    "official_evidence_url": review_source_url(
                        final_record, v1_record, field
                    ),
                    "human_v1_result": prior.get("human_v1_result", ""),
                    "human_final_result": prior.get("human_final_result", ""),
                    "human_correction": prior.get("human_correction", ""),
                    "human_notes": prior.get("human_notes", ""),
                }
            )
    return rows


def write_review_csv(rows: list[dict[str, str]]) -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with REVIEW_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def load_review_rows() -> list[dict[str, str]]:
    if not REVIEW_PATH.exists():
        raise FileNotFoundError(f"Human review sheet is missing: {REVIEW_PATH}")
    with REVIEW_PATH.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != REVIEW_COLUMNS:
            raise ValueError(f"{REVIEW_PATH} has an unexpected header")
        rows = list(reader)
    expected = {(str(app_id), field) for app_id in HOLDOUT_IDS for field in REVIEW_FIELDS}
    actual = {(row.get("app_id", ""), row.get("field", "")) for row in rows}
    if len(rows) != 70 or actual != expected:
        raise ValueError("human_review.csv must contain exactly the fixed 70 holdout field rows")
    return rows


def review_is_complete(rows: list[dict[str, str]]) -> bool:
    return len(rows) == len(HOLDOUT_IDS) * len(REVIEW_FIELDS) and all(
        row.get(column, "").strip().lower() in VALID_HUMAN_RESULTS
        for row in rows
        for column in ("human_v1_result", "human_final_result")
    )


def _accuracy(rows: list[dict[str, str]], column: str) -> dict[str, int | float]:
    values = [row.get(column, "").strip().lower() for row in rows]
    counts = Counter(values)
    reviewed = sum(counts[result] for result in VALID_HUMAN_RESULTS)
    correct = counts["correct"]
    return {
        "correct": correct,
        "incorrect": counts["incorrect"],
        "unclear": counts["unclear"],
        "reviewed": reviewed,
        "accuracy": round(correct / reviewed, 4) if reviewed else 0.0,
    }


def _evidence_items(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for record in records for item in record.get("evidence", []) if isinstance(item, dict)]


def _value_is_unresolved(record: dict[str, Any], field: str) -> bool:
    if field == "description":
        description = str(record.get("description") or "").strip().lower()
        return not description or description == "unknown" or description.startswith("unknown —")
    value = field_value(record, field).split(" — ", 1)[0].strip().lower()
    return value in {"unknown", "none_found"}


def _blocker(record: dict[str, Any]) -> str:
    value = str(_nested(record, "buildability", "main_blocker", default="") or "").strip()
    return value if value and value.lower() not in {"unknown", "none_found", "none", "no material blocker"} else ""


def _blocker_bucket(record: dict[str, Any]) -> str:
    verdict = str(_nested(record, "buildability", "verdict", default="unknown"))
    access = str(_nested(record, "credential_access", "status", default="unknown"))
    breadth = str(_nested(record, "api_surface", "breadth", default="unknown"))
    blocker = _blocker(record).lower()
    if access == "partner_gated" or "partner" in blocker or "contact sales" in blocker:
        return "Partner or sales approval"
    if access == "admin_gated" or "admin" in blocker:
        return "Admin approval or setup"
    if access == "paid" or any(word in blocker for word in ("paid", "payment", "plan", "subscription")):
        return "Paid plan required"
    if breadth == "narrow" or "narrow" in blocker or "limited" in blocker:
        return "Narrow API surface"
    if verdict == "ready":
        if access == "unknown":
            return "Credential access unresolved"
        if blocker:
            return "Other documented constraint"
        return "No documented blocker"
    if verdict == "unknown" or breadth == "unknown":
        return "Insufficient official evidence"
    if breadth == "none_found":
        return "No usable public API found"
    if "credential" in blocker or "oauth" in blocker or "token" in blocker:
        return "Credential access constraint"
    return "Other documented constraint"


def _verification_statuses(payload: Any) -> dict[str, bool]:
    """Collect URL-level resolution outcomes from the verifier without assuming its envelope."""
    statuses: dict[str, bool] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            url = value.get("url") or value.get("requested_url")
            status = value.get("http_status")
            resolved = value.get("resolved")
            if isinstance(url, str):
                if isinstance(resolved, bool):
                    statuses[url] = statuses.get(url, False) or resolved
                elif isinstance(status, int):
                    statuses[url] = statuses.get(url, False) or 200 <= status < 400
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return statuses


def _verification_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    results = [row for row in payload.get("results", []) if isinstance(row, dict)]
    audits = [row for row in payload.get("app_audits", []) if isinstance(row, dict)]
    browser_rows = [
        row
        for row in payload.get("independent_browser_review", [])
        if isinstance(row, dict)
    ]
    expected_browser = {
        (app_id, field) for app_id in HOLDOUT_IDS for field in REVIEW_FIELDS
    }
    actual_browser = {
        (row.get("app_id"), row.get("field")) for row in browser_rows
    }
    return {
        "url_check_events": len(results),
        "resolved_url_events": sum(row.get("resolved") is True for row in results),
        "app_audits": len(audits),
        "initial_failed_fields": sum(
            len(row.get("initial_failed_fields", [])) for row in audits
        ),
        "final_failed_fields": sum(
            len(row.get("final_failed_fields", [])) for row in audits
        ),
        "corrected_apps": sum(bool(row.get("changed_fields")) for row in audits),
        "corrected_fields": sum(len(row.get("changed_fields", [])) for row in audits),
        "browser": {
            "complete": actual_browser == expected_browser,
            "checks": len(browser_rows),
            "expected_checks": len(expected_browser),
            "apps": len({row.get("app_id") for row in browser_rows}),
            "outcomes": dict(
                sorted(
                    Counter(
                        str(row.get("visible_support") or "unknown")
                        for row in browser_rows
                    ).items()
                )
            ),
        },
    }


def _field_comparison_value(record: dict[str, Any], field: str) -> Any:
    """Match the verifier's definition of one changed assignment field."""
    if field == "description":
        return record.get("description")
    if field == "auth_methods":
        return record.get("auth_methods")
    if field == "credential_access":
        return record.get("credential_access")
    if field == "api_styles":
        return _nested(record, "api_surface", "styles", default=[])
    if field == "api_breadth":
        return _nested(record, "api_surface", "breadth", default="unknown")
    if field == "mcp":
        return record.get("mcp")
    if field == "buildability":
        return record.get("buildability")
    raise ValueError(f"Unknown comparison field: {field}")


def compute_metrics(
    records: list[dict[str, Any]],
    v1_records: list[dict[str, Any]] | None = None,
    review_rows: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Compute every displayed total from artifacts, in deterministic key order."""
    records = sorted(records, key=lambda record: int(record.get("id", 0)))
    _require_full_research_set(records, "Report source")
    evidence = _evidence_items(records)
    verification_payload = _read_json(VERIFICATION_PATH) if VERIFICATION_PATH.exists() else {}
    verification = _verification_statuses(verification_payload)
    citation_resolved = sum(
        verification.get(str(item.get("url")), 200 <= int(item.get("http_status") or 0) < 400)
        for item in evidence
    )
    source_types = Counter(str(item.get("source_type") or "unknown") for item in evidence)
    auth = Counter(method for record in records for method in record.get("auth_methods", []) if method)
    access = Counter(str(_nested(record, "credential_access", "status", default="unknown") or "unknown") for record in records)
    buildability = Counter(str(_nested(record, "buildability", "verdict", default="unknown") or "unknown") for record in records)
    mcp = Counter(str(_nested(record, "mcp", "status", default="unknown") or "unknown") for record in records)
    categories: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("category") or "Unknown")].append(record)
    for category in sorted(grouped):
        category_records = grouped[category]
        categories[category] = {
            "total": len(category_records),
            "access": dict(sorted(Counter(str(_nested(row, "credential_access", "status", default="unknown") or "unknown") for row in category_records).items())),
            "buildability": dict(sorted(Counter(str(_nested(row, "buildability", "verdict", default="unknown") or "unknown") for row in category_records).items())),
        }
    unresolved = sum(_value_is_unresolved(record, field) for record in records for field in REVIEW_FIELDS)
    low_confidence = sum(
        1
        for record in records
        for field in REVIEW_FIELDS
        if str((record.get("confidence") or {}).get(field) or "").lower() == "low"
    )
    easy_wins = [
        record["id"] for record in records
        if _nested(record, "buildability", "verdict", default="unknown") == "ready"
        and _nested(record, "credential_access", "status", default="unknown") in SELF_SERVE_ACCESS
    ]
    outreach = [
        record["id"] for record in records
        if _nested(record, "credential_access", "status", default="unknown") in {"admin_gated", "partner_gated"}
        and str(_nested(record, "api_surface", "breadth", default="unknown")) not in {"none_found", "unknown"}
    ]
    changed = 0
    correction_examples: list[dict[str, Any]] = []
    if v1_records:
        v1_by_id = {record["id"]: record for record in v1_records}
        for record in records:
            changed_fields = [
                field
                for field in REVIEW_FIELDS
                if _field_comparison_value(record, field)
                != _field_comparison_value(v1_by_id.get(record["id"], {}), field)
            ]
            changed += len(changed_fields)
            if changed_fields:
                correction_examples.append(
                    {"app_id": record["id"], "app_name": record["name"], "fields": changed_fields}
                )
    unresolved_cases = [
        {
            "app_id": record["id"],
            "app_name": record["name"],
            "fields": [field for field in REVIEW_FIELDS if _value_is_unresolved(record, field)],
        }
        for record in records
        if any(_value_is_unresolved(record, field) for field in REVIEW_FIELDS)
    ]
    known_auth = {key: value for key, value in auth.items() if key != "unknown"}
    top_auth = max(known_auth, key=known_auth.get) if known_auth else "unknown"
    top_auth_label = {
        "oauth2": "OAuth2",
        "api_key": "API key",
    }.get(top_auth, top_auth.replace("_", " "))
    ready_count = buildability.get("ready", 0)
    metrics: dict[str, Any] = {
        "app_count": len(records),
        "evidence": {
            "citation_count": len(evidence),
            "citation_resolution_rate": round(citation_resolved / len(evidence), 4) if evidence else 0.0,
            "claim_support_rate": round(sum(bool(item.get("claim_supported")) for item in evidence) / len(evidence), 4) if evidence else 0.0,
            "first_party_rate": round(sum(bool(item.get("official_source")) for item in evidence) / len(evidence), 4) if evidence else 0.0,
            "source_types": dict(sorted(source_types.items())),
        },
        "unresolved": {"field_count": unresolved, "field_rate": round(unresolved / (len(records) * len(REVIEW_FIELDS)), 4), "low_confidence_field_count": low_confidence},
        "distributions": {"auth_methods": dict(sorted(auth.items())), "credential_access": dict(sorted(access.items())), "buildability": dict(sorted(buildability.items())), "mcp": dict(sorted(mcp.items()))},
        "categories": categories,
        "headline": f"{ready_count} of {len(records)} apps receive a ready verdict; {top_auth_label} is the most documented auth method",
        "blockers": dict(Counter(_blocker_bucket(record) for record in records).most_common()),
        "easy_win_app_ids": easy_wins,
        "outreach_candidate_app_ids": outreach,
        "corrections_between_v1_and_final": changed,
        "correction_examples": correction_examples,
        "unresolved_cases": unresolved_cases,
        "composio_catalog_searches": sum(bool(_nested(record, "composio_coverage", "searched", default=False)) for record in records),
        "automated_verification": _verification_summary(verification_payload),
    }
    if review_rows is not None and review_is_complete(review_rows):
        metrics["human_review"] = {
            "complete": True,
            "v1": _accuracy(review_rows, "human_v1_result"),
            "final": _accuracy(review_rows, "human_final_result"),
        }
    else:
        metrics["human_review"] = {"complete": False, "status": "pending"}
    return metrics


def _json_for_html(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render_html(records: list[dict[str, Any]], metrics: dict[str, Any], review_complete: bool) -> str:
    """Render one dependency-light HTML file; all dynamic text uses textContent."""
    table_rows = []
    for record in records:
        evidence = {field: evidence_url(record, field) for field in REVIEW_FIELDS}
        table_rows.append({
            "id": record.get("id"), "name": record.get("name", ""), "category": record.get("category", ""),
            "description": field_value(record, "description"), "auth": field_value(record, "auth_methods"),
            "access": field_value(record, "credential_access"),
            "api_style": field_value(record, "api_styles"), "api_breadth": field_value(record, "api_breadth"),
            "mcp": field_value(record, "mcp"), "buildability": field_value(record, "buildability"),
            "evidence": evidence,
        })
    state = {"records": table_rows, "metrics": metrics, "reviewComplete": review_complete}
    title = "100-app API research: verified final results" if review_complete else "100-app API research: human review pending"
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
  <style>
    html {{ scroll-behavior: smooth; }}
    body {{ background: radial-gradient(circle at 12% 0%, #eef2ff 0, transparent 32rem), radial-gradient(circle at 92% 12%, #ecfeff 0, transparent 28rem), #f8fafc; }}
    ::selection {{ background: #c7d2fe; color: #312e81; }}
  </style>
</head>
<body class="min-h-screen text-slate-700 antialiased">
  <nav class="sticky top-0 z-20 border-b border-white/80 bg-white/85 backdrop-blur-xl">
    <div class="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6 lg:px-8">
      <a href="#top" class="text-sm font-semibold tracking-tight text-slate-950">Composio research lab</a>
      <div class="flex items-center gap-4 text-xs font-medium text-slate-500"><a class="hover:text-indigo-700" href="#findings">Findings</a><a class="hover:text-indigo-700" href="#proof">Proof</a><a class="hover:text-indigo-700" href="#dataset">Dataset</a></div>
    </div>
  </nav>
  <main id="top" class="mx-auto max-w-7xl px-4 pb-16 pt-6 sm:px-6 lg:px-8">
    <header class="relative overflow-hidden rounded-[2rem] border border-indigo-100 bg-white/90 px-6 py-10 shadow-xl shadow-indigo-100/60 sm:px-10 sm:py-14">
      <div class="absolute -right-20 -top-24 h-64 w-64 rounded-full bg-cyan-100/70 blur-3xl"></div>
      <div class="absolute -bottom-24 left-1/3 h-52 w-52 rounded-full bg-indigo-100/80 blur-3xl"></div>
      <div class="relative max-w-4xl">
        <div class="flex flex-wrap items-center gap-2"><span class="rounded-full bg-indigo-50 px-3 py-1 text-xs font-semibold tracking-wide text-indigo-700">EVIDENCE-FIRST · AUG 2026</span><span id="review-status" class="rounded-full px-3 py-1 text-xs font-medium"></span></div>
        <h1 id="headline" class="mt-6 max-w-4xl text-4xl font-semibold tracking-[-0.035em] text-slate-950 sm:text-5xl lg:text-6xl"></h1>
        <p id="subtitle" class="mt-5 max-w-3xl text-base leading-7 text-slate-600 sm:text-lg"></p>
        <div id="hero-facts" class="mt-8 flex flex-wrap gap-2"></div>
      </div>
    </header>

    <section aria-label="Computed metrics" class="-mt-4 grid gap-3 px-3 sm:grid-cols-2 lg:grid-cols-5" id="metric-cards"></section>

    <section id="findings" class="mt-14">
      <div class="max-w-2xl"><p class="text-xs font-semibold tracking-[0.18em] text-indigo-700">THE DECISION LAYER</p><h2 class="mt-2 text-3xl font-semibold tracking-tight text-slate-950">Where to build first—and where to ask for access</h2><p class="mt-3 leading-7 text-slate-600">Counts below come directly from the corrected artifact. Unknowns remain visible instead of being forced into a positive category.</p></div>
      <div class="mt-7 grid gap-5 lg:grid-cols-[1.05fr_.95fr]">
        <article class="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm"><div class="flex items-start justify-between gap-4"><div><h3 class="text-lg font-semibold text-slate-950">Auth, access and MCP</h3><p class="mt-1 text-sm text-slate-500">Multi-auth apps appear in more than one auth row.</p></div><span class="rounded-xl bg-indigo-50 px-3 py-1 text-xs font-medium text-indigo-700">portfolio view</span></div><div id="patterns" class="mt-6 grid gap-6 sm:grid-cols-3"></div></article>
        <article class="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm"><h3 class="text-lg font-semibold text-slate-950">Action queues</h3><p class="mt-1 text-sm text-slate-500">Evidence-backed shortlists, generated by the rubric.</p><div id="queue" class="mt-5 grid gap-4 sm:grid-cols-2"></div></article>
      </div>
    </section>

    <section id="proof" class="mt-14 rounded-[2rem] border border-slate-200 bg-slate-950 p-6 text-slate-100 shadow-xl shadow-slate-200 sm:p-8">
      <div class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between"><div><p class="text-xs font-semibold tracking-[0.18em] text-cyan-300">HOW TRUST WAS BUILT</p><h2 class="mt-2 text-3xl font-semibold tracking-tight">One loop, three independent checks</h2></div><p class="max-w-xl text-sm leading-6 text-slate-300">Search discovered candidates; only directly fetched passages became evidence. Browser outcomes are non-human and are reported separately from the applicant audit.</p></div>
      <div id="workflow" class="mt-7 grid gap-3 md:grid-cols-4"></div>
      <div class="mt-6 grid gap-4 lg:grid-cols-2"><div class="rounded-2xl border border-white/10 bg-white/5 p-5"><h3 class="font-semibold text-white">Rendered-page holdout</h3><div id="browser-proof" class="mt-3 text-sm leading-6 text-slate-300"></div></div><div class="rounded-2xl border border-white/10 bg-white/5 p-5"><h3 class="font-semibold text-white">Measured accuracy</h3><div id="accuracy" class="mt-3 space-y-2 text-sm leading-6 text-slate-300"></div></div></div>
    </section>

    <section class="mt-14 grid gap-5 lg:grid-cols-2">
      <article class="rounded-3xl border border-emerald-100 bg-emerald-50/70 p-6"><p class="text-xs font-semibold tracking-[0.16em] text-emerald-700">CORRECTIONS</p><h2 class="mt-2 text-xl font-semibold text-slate-950">What changed after verification</h2><div id="corrections" class="mt-4 grid gap-2 text-sm text-slate-700"></div></article>
      <article class="rounded-3xl border border-amber-100 bg-amber-50/70 p-6"><p class="text-xs font-semibold tracking-[0.16em] text-amber-700">HONEST GAPS</p><h2 class="mt-2 text-xl font-semibold text-slate-950">Where evidence remained insufficient</h2><div id="unresolved-cases" class="mt-4 grid gap-2 text-sm text-slate-700"></div></article>
    </section>

    <section class="mt-14 rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
      <div><p class="text-xs font-semibold tracking-[0.16em] text-indigo-700">CATEGORY MAP</p><h2 class="mt-2 text-2xl font-semibold text-slate-950">Access and buildability by category</h2></div>
      <div class="mt-5 overflow-x-auto"><table class="min-w-full text-left text-sm"><thead class="border-b border-slate-200 text-xs tracking-wide text-slate-500"><tr><th class="px-3 py-3 font-medium">Category</th><th class="px-3 py-3 font-medium">Apps</th><th class="px-3 py-3 font-medium">Credential access</th><th class="px-3 py-3 font-medium">Buildability verdicts</th></tr></thead><tbody id="category-table"></tbody></table></div>
    </section>

    <section id="dataset" class="mt-14 rounded-3xl border border-slate-200 bg-white p-5 shadow-sm sm:p-6">
      <div class="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between"><div><p class="text-xs font-semibold tracking-[0.16em] text-indigo-700">AUDITABLE DATASET</p><h2 id="dataset-heading" class="mt-2 text-2xl font-semibold text-slate-950"></h2><p class="mt-1 text-sm text-slate-500">Every visible positive claim links back to a recorded source.</p></div><div class="flex flex-col gap-2 sm:flex-row"><input id="search" class="w-full rounded-xl border border-slate-300 bg-slate-50 px-4 py-2.5 text-sm outline-none ring-indigo-200 focus:ring sm:w-72" placeholder="Search apps, auth or blockers"><select id="category-filter" class="rounded-xl border border-slate-300 bg-slate-50 px-4 py-2.5 text-sm outline-none ring-indigo-200 focus:ring"><option value="">All categories</option></select></div></div>
      <div class="mt-5 overflow-x-auto"><table class="min-w-[1180px] text-left text-sm"><thead class="sticky top-11 bg-white text-xs tracking-wide text-slate-500"><tr class="border-y border-slate-200"><th class="px-3 py-3 font-medium">App</th><th class="px-3 py-3 font-medium">Category</th><th class="px-3 py-3 font-medium">Auth</th><th class="px-3 py-3 font-medium">Access</th><th class="px-3 py-3 font-medium">API breadth / style</th><th class="px-3 py-3 font-medium">MCP</th><th class="px-3 py-3 font-medium">Buildability</th><th class="px-3 py-3 font-medium">Evidence</th></tr></thead><tbody id="app-table"></tbody></table></div>
      <p id="table-count" class="mt-4 text-sm text-slate-500"></p>
    </section>

    <footer class="mt-10 rounded-3xl border border-slate-200 bg-white/80 p-6 text-sm leading-6 text-slate-500"><p class="font-medium text-slate-800">Reproduce locally</p><p class="mt-1"><code class="rounded bg-slate-100 px-2 py-1 text-xs text-slate-700">uv run python -m research_agent.report --prepare-review</code> → complete the fixed CSV → <code class="rounded bg-slate-100 px-2 py-1 text-xs text-slate-700">uv run python -m research_agent.report --final</code></p><p class="mt-3 text-xs">Tailwind’s browser CDN is the only UI dependency, so styling requires network access. Repository and live links remain intentionally absent until real authorization exists.</p></footer>
  </main>
  <script>
  const state = {_json_for_html(state)};
  const add = (parent, tag, classes, text) => {{ const node = document.createElement(tag); node.className = classes; node.textContent = text; parent.append(node); return node; }};
  const metrics = state.metrics;
  const evidence = metrics.evidence;
  const verification = metrics.automated_verification;
  const percent = value => `${{(Number(value || 0) * 100).toFixed(1)}}%`;
  const label = value => String(value).replaceAll('_', ' ');
  const count = values => Object.entries(values || {{}}).map(([key, value]) => `${{label(key)}} ${{value}}`).join(' · ') || 'none recorded';
  const appNames = ids => ids.map(id => state.records.find(row => row.id === id)?.name).filter(Boolean);
  document.getElementById('headline').textContent = metrics.headline;
  document.getElementById('subtitle').textContent = `A runnable agent mapped ${{metrics.app_count}} requested apps across ${{Object.keys(metrics.categories).length}} categories, then re-fetched every final citation and corrected unsupported fields.`;
  const review = document.getElementById('review-status');
  review.className = state.reviewComplete ? 'rounded-full bg-emerald-100 px-3 py-1 text-xs font-medium text-emerald-800' : 'rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-800';
  review.textContent = state.reviewComplete ? 'Human review complete' : 'Human review pending — accuracy withheld';
  const heroFacts = document.getElementById('hero-facts'); [[`${{evidence.citation_count.toLocaleString()}}`, 'citations'], [metrics.composio_catalog_searches, 'Composio catalog checks'], [verification.corrected_apps, 'apps corrected'], [verification.browser.checks, 'browser holdout checks']].forEach(([value, text]) => {{ const chip = document.createElement('span'); chip.className = 'rounded-full border border-slate-200 bg-white/80 px-3 py-1.5 text-xs text-slate-600'; chip.textContent = `${{value}} ${{text}}`; heroFacts.append(chip); }});
  const cards = [['Ready verdicts', metrics.distributions.buildability.ready || 0, 'of the corrected set'], ['Final citations', evidence.citation_count.toLocaleString(), 'claim-level sources'], ['Citation resolution', percent(evidence.citation_resolution_rate), 'final evidence URLs'], ['First-party evidence', percent(evidence.first_party_rate), 'citation-level'], ['Unresolved fields', `${{metrics.unresolved.field_count}} / ${{metrics.app_count * 7}}`, 'kept visible']];
  const cardRoot = document.getElementById('metric-cards'); cards.forEach(([name, value, note]) => {{ const card = document.createElement('article'); card.className = 'rounded-2xl border border-slate-200 bg-white p-5 shadow-lg shadow-slate-200/50'; add(card, 'p', 'text-xs font-medium tracking-wide text-slate-500', name.toUpperCase()); add(card, 'p', 'mt-2 text-2xl font-semibold tracking-tight text-slate-950', String(value)); add(card, 'p', 'mt-1 text-xs text-slate-500', note); cardRoot.append(card); }});
  function barList(root, title, values, color) {{ const panel = document.createElement('div'); add(panel, 'p', 'mb-3 text-xs font-semibold tracking-wide text-slate-700', title.toUpperCase()); const max = Math.max(...Object.values(values), 1); Object.entries(values).sort((a,b) => b[1]-a[1]).forEach(([name,value]) => {{ const line = document.createElement('div'); line.className = 'mb-2'; const top = document.createElement('div'); top.className = 'flex justify-between text-xs text-slate-600'; add(top, 'span', '', label(name)); add(top, 'span', 'font-medium text-slate-800', String(value)); line.append(top); const track = document.createElement('div'); track.className = 'mt-1 h-1.5 overflow-hidden rounded-full bg-slate-100'; const fill = document.createElement('div'); fill.className = `h-full rounded-full ${{color}}`; fill.style.width = `${{Math.max(4, value/max*100)}}%`; track.append(fill); line.append(track); panel.append(line); }}); root.append(panel); }}
  const patterns = document.getElementById('patterns'); barList(patterns, 'Auth methods', metrics.distributions.auth_methods, 'bg-indigo-500'); barList(patterns, 'Credential access', metrics.distributions.credential_access, 'bg-cyan-500'); barList(patterns, 'MCP status', metrics.distributions.mcp, 'bg-violet-500');
  const queue = document.getElementById('queue'); [['Easy wins', metrics.easy_win_app_ids, 'Ready verdict + free or trial', 'border-emerald-200 bg-emerald-50'], ['Outreach', metrics.outreach_candidate_app_ids, 'Useful API + access gate', 'border-amber-200 bg-amber-50']].forEach(([name, ids, rule, classes]) => {{ const box = document.createElement('div'); box.className = `rounded-2xl border p-4 ${{classes}}`; add(box, 'p', 'font-semibold text-slate-950', `${{name}} · ${{ids.length}}`); add(box, 'p', 'mt-1 text-xs text-slate-500', rule); add(box, 'p', 'mt-3 text-xs leading-5 text-slate-700', appNames(ids).join(' · ') || 'None'); queue.append(box); }});
  const workflow = document.getElementById('workflow'); [['01', 'Discover', 'Web search finds official candidates; snippets are discarded.'], ['02', 'Fetch + extract', 'Direct HTTP or browser text becomes strict claim-level evidence.'], ['03', 'Fresh critic', `${{verification.app_audits}} app audits; ${{verification.initial_failed_fields}} weak fields entered correction.`], ['04', 'Re-check', `${{verification.url_check_events.toLocaleString()}} URL events; ${{verification.final_failed_fields}} deterministic failures remain.`]].forEach(([step, name, copy]) => {{ const box = document.createElement('div'); box.className = 'rounded-2xl border border-white/10 bg-white/5 p-4'; add(box, 'p', 'text-xs font-medium text-cyan-300', step); add(box, 'p', 'mt-2 font-semibold text-white', name); add(box, 'p', 'mt-2 text-xs leading-5 text-slate-300', copy); workflow.append(box); }});
  const browserProof = document.getElementById('browser-proof'); const bo = verification.browser.outcomes; add(browserProof, 'p', '', `${{verification.browser.checks}} / ${{verification.browser.expected_checks}} fixed checks attempted across ${{verification.browser.apps}} apps.`); add(browserProof, 'p', 'mt-2', `Rendered support ${{bo.supported || 0}} · unresolved ${{bo.unresolved || 0}} · passage not visible ${{bo.not_visible || 0}} · load blocked/failed ${{bo.load_failed || 0}}.`); add(browserProof, 'p', 'mt-2 text-xs text-slate-400', 'These are independent browser outcomes, not human labels. Otter’s help center returned 403 in the browser pass.');
  const accuracy = document.getElementById('accuracy'); if (state.reviewComplete) {{ const human = metrics.human_review; [['V1 accuracy', human.v1], ['Final accuracy', human.final]].forEach(([label, value]) => add(accuracy, 'p', '', `${{label}}: ${{percent(value.accuracy)}} (${{value.correct}} correct / ${{value.reviewed}} reviewed; ${{value.unclear}} unclear included in the denominator).`)); }} else {{ add(accuracy, 'p', '', 'The fixed 70-field human holdout has not been completed. No V1 or final accuracy is claimed.'); }} add(accuracy, 'p', '', `V1 → final changes: ${{metrics.corrections_between_v1_and_final}} fields. Unresolved: ${{metrics.unresolved.field_count}} fields; low confidence: ${{metrics.unresolved.low_confidence_field_count}} fields.`);
  const corrections = document.getElementById('corrections'); if (metrics.correction_examples.length) {{ metrics.correction_examples.slice(0, 10).forEach(item => add(corrections, 'p', 'rounded-xl bg-white/70 px-3 py-2', `${{item.app_id}}. ${{item.app_name}} · ${{item.fields.map(label).join(', ')}}`)); add(corrections, 'p', 'mt-1 text-xs text-slate-500', `${{verification.corrected_apps}} apps / ${{verification.corrected_fields}} fields changed in the full verifier artifact.`); }} else {{ add(corrections, 'p', '', 'No V1 → final field changes were recorded.'); }}
  const unresolvedCases = document.getElementById('unresolved-cases'); if (metrics.unresolved_cases.length) {{ metrics.unresolved_cases.slice(0, 10).forEach(item => add(unresolvedCases, 'p', 'rounded-xl bg-white/70 px-3 py-2', `${{item.app_id}}. ${{item.app_name}} · ${{item.fields.map(label).join(', ')}}`)); if (metrics.unresolved_cases.length > 10) add(unresolvedCases, 'p', 'mt-1 text-xs text-slate-500', `Plus ${{metrics.unresolved_cases.length - 10}} more apps in the searchable dataset.`); }} else {{ add(unresolvedCases, 'p', '', 'Every field has supported evidence.'); }}
  const categoryRoot = document.getElementById('category-table'); Object.entries(metrics.categories).forEach(([name, value]) => {{ const tr = document.createElement('tr'); tr.className = 'border-b border-slate-100 hover:bg-slate-50/80'; [name, value.total, count(value.access), count(value.buildability)].forEach((cell,index) => add(tr, 'td', `px-3 py-3.5 align-top ${{index===0 ? 'font-medium text-slate-900' : 'text-slate-600'}}`, String(cell))); categoryRoot.append(tr); }});
  const categoryFilter = document.getElementById('category-filter'); [...new Set(state.records.map(row => row.category))].sort().forEach(category => {{ const option = document.createElement('option'); option.value = category; option.textContent = category; categoryFilter.append(option); }});
  document.getElementById('dataset-heading').textContent = `All ${{state.records.length}} apps, source-linked`;
  const table = document.getElementById('app-table');
  function renderTable() {{ const query = document.getElementById('search').value.trim().toLowerCase(); const category = categoryFilter.value; const visible = state.records.filter(row => (!category || row.category === category) && JSON.stringify(row).toLowerCase().includes(query)); table.replaceChildren(); visible.forEach(row => {{ const tr = document.createElement('tr'); tr.className = 'border-b border-slate-100 hover:bg-indigo-50/30'; [[`${{row.id}}. ${{row.name}}`, row.description], [row.category], [row.auth], [row.access], [row.api_breadth, row.api_style], [row.mcp], [row.buildability]].forEach((values,index) => {{ const td = document.createElement('td'); td.className = 'px-3 py-4 align-top text-slate-600'; add(td, 'div', index===0 ? 'font-semibold text-slate-950' : 'font-medium text-slate-800', values[0]); if (values[1]) add(td, 'p', 'mt-1 max-w-sm text-xs leading-5 text-slate-500', values[1]); tr.append(td); }}); const evidenceCell = document.createElement('td'); evidenceCell.className = 'px-3 py-4 align-top'; Object.entries(row.evidence).filter(([, url]) => url).forEach(([field, url]) => {{ const link = document.createElement('a'); link.href = url; link.target = '_blank'; link.rel = 'noreferrer'; link.className = 'mb-1 mr-1 inline-flex rounded-md bg-indigo-50 px-2 py-1 text-[11px] font-medium text-indigo-700 hover:bg-indigo-100'; link.textContent = label(field); evidenceCell.append(link); }}); if (!evidenceCell.childNodes.length) add(evidenceCell, 'span', 'text-xs text-slate-400', 'No supported link'); tr.append(evidenceCell); table.append(tr); }}); document.getElementById('table-count').textContent = `${{visible.length}} of ${{state.records.length}} apps shown`; }}
  document.getElementById('search').addEventListener('input', renderTable); categoryFilter.addEventListener('change', renderTable); renderTable();
  </script>
</body>
</html>'''


def write_report(records: list[dict[str, Any]], metrics: dict[str, Any], review_complete: bool) -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    with METRICS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    (SITE_DIR / "index.html").write_text(render_html(records, metrics, review_complete), encoding="utf-8")


def prepare_review() -> None:
    load_manifest()  # Catch a changed assignment list before preparing its fixed holdout.
    v1_records = load_records(V1_PATH)
    final_records = load_records(FINAL_PATH) if FINAL_PATH.exists() else v1_records
    _require_full_research_set(v1_records, "V1")
    _require_full_research_set(final_records, "Final report source")
    rows = build_review_rows(v1_records, final_records)
    write_review_csv(rows)
    metrics = compute_metrics(final_records, v1_records, rows)
    write_report(final_records, metrics, review_complete=False)


def finalize_report() -> None:
    load_manifest()
    v1_records = load_records(V1_PATH)
    final_records = load_records(FINAL_PATH)
    _require_full_research_set(v1_records, "V1")
    _require_full_research_set(final_records, "Final")
    rows = load_review_rows()
    if not review_is_complete(rows):
        raise ValueError("Human review is incomplete: fill both human result columns for all 70 holdout rows")
    metrics = compute_metrics(final_records, v1_records, rows)
    write_report(final_records, metrics, review_complete=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create computed research metrics and the static case-study page.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare-review", action="store_true", help="create/update the fixed human holdout sheet and draft site")
    mode.add_argument("--final", action="store_true", help="build final metrics/site after genuine human review")
    args = parser.parse_args()
    if args.prepare_review:
        prepare_review()
        print(f"Prepared {REVIEW_PATH} and {SITE_DIR / 'index.html'}")
    else:
        finalize_report()
        print(f"Wrote {METRICS_PATH} and {SITE_DIR / 'index.html'}")


if __name__ == "__main__":
    main()
