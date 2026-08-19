from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

# Keep collection independent of whether the repository has been installed as
# a package (the scaffold intentionally has no build-system package section).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_agent.models import (
    MAJOR_FIELDS,
    AppInput,
    ResearchRecord,
)


MANIFEST_PATH = ROOT / "data" / "apps.json"

PILOT_IDS = (1, 11, 21, 31, 41, 51, 61, 71, 81, 91)
HOLDOUT_IDS = (2, 12, 22, 32, 42, 52, 62, 72, 82, 92)
EXPECTED_CATEGORIES = {
    "CRM and Sales",
    "Support and Helpdesk",
    "Communications and Messaging",
    "Marketing, Ads, Email and Social",
    "Ecommerce",
    "Data, SEO and Scraping",
    "Developer, Infra and Data platforms",
    "Productivity and Project Management",
    "Finance and Fintech",
    "AI, Research and Media-native",
}

CONFIDENT = {"high", "medium"}
UNKNOWN_MARKERS = {"unknown", "none_found"}
EVIDENCE_FIELDS = set(MAJOR_FIELDS)
HUMAN_FIELDS = {
    "description",
    "auth_methods",
    "credential_access",
    "api_styles",
    "api_breadth",
    "mcp",
    "buildability",
}
HUMAN_REVIEW_COLUMNS = {
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
}
ALLOWED_HUMAN_RESULTS = {"", "correct", "incorrect", "unclear"}


def _manifest() -> list[dict[str, Any]]:
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, list), "data/apps.json must be a JSON array"
    return value


def _records_payload(path: Path) -> list[dict[str, Any]]:
    """Read either the documented list form or a records wrapper."""
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        records = payload["records"]
    else:
        raise AssertionError(f"{path} must contain a list of records")
    assert all(isinstance(record, dict) for record in records)
    return records


def _field_value(record: ResearchRecord, field: str) -> Any:
    if field == "api_styles" or field == "api_breadth":
        return getattr(record.api_surface, field.removeprefix("api_"))
    return getattr(record, field)


def _is_unknown(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized in UNKNOWN_MARKERS or normalized.startswith("unknown —")
    if isinstance(value, list):
        return not value or all(_is_unknown(item) for item in value)
    if hasattr(value, "value"):
        return _is_unknown(value.value)
    if hasattr(value, "status"):
        return _is_unknown(value.status)
    if hasattr(value, "breadth"):
        return _is_unknown(value.breadth)
    if hasattr(value, "verdict"):
        return _is_unknown(value.verdict)
    return False


def _assert_evidence_contract(record: ResearchRecord) -> None:
    """Check claim-level evidence and the first-party source policy."""
    evidence_by_field: dict[str, list[Any]] = {field: [] for field in EVIDENCE_FIELDS}
    for evidence in record.evidence:
        assert evidence.field in EVIDENCE_FIELDS
        assert evidence.http_status >= 200
        assert evidence.supporting_text.strip()
        assert str(evidence.url).startswith(("http://", "https://"))
        evidence_by_field[evidence.field].append(evidence)

        if evidence.field != "mcp":
            assert evidence.official_source is True
            assert evidence.source_type != "community_repository"
        elif evidence.source_type == "community_repository":
            assert evidence.official_source is False
            assert record.mcp.status.value == "community"
        else:
            assert evidence.official_source is True

    for field in MAJOR_FIELDS:
        mapped = evidence_by_field[field]
        supported = [item for item in mapped if item.claim_supported]
        if _is_unknown(_field_value(record, field)):
            continue
        assert supported, f"positive {field} claim has no supporting evidence"


def _valid_record_data() -> dict[str, Any]:
    evidence = [
        {
            "field": field,
            "url": f"https://docs.example.test/{field}",
            "page_title": f"{field} documentation",
            "supporting_text": f"Official documentation supports the {field} claim.",
            "source_type": "official_docs",
            "fetched_at": "2026-08-19T00:00:00Z",
            "http_status": 200,
            "official_source": True,
            "claim_supported": True,
        }
        for field in MAJOR_FIELDS
    ]
    return {
        "id": 1,
        "name": "Example CRM",
        "category": "CRM and Sales",
        "website_hint": "example.test",
        "description": "A documented customer relationship management API.",
        "auth_methods": ["oauth2"],
        "credential_access": {"status": "free", "requirements": "Create a developer account."},
        "api_surface": {
            "styles": ["rest"],
            "breadth": "broad",
            "notes": "The API reference covers the principal resources.",
        },
        "mcp": {
            "status": "none_found",
            "url": None,
            "notes": "No documented MCP implementation found.",
        },
        "buildability": {
            "verdict": "ready",
            "reason": "Credentials and a documented API are available.",
            "main_blocker": "none_found",
        },
        "confidence": {field: "high" for field in MAJOR_FIELDS},
        "evidence": evidence,
        "unresolved_issues": [],
        "composio_coverage": {
            "searched": True,
            "toolkit_found": False,
            "toolkit_slug": None,
            "relevant_tools": [],
            "notes": "Catalog checked.",
        },
        "attempt_count": 1,
        "status": "complete",
    }


def _assert_record_set(path: Path, manifest: list[dict[str, Any]]) -> list[ResearchRecord]:
    records = _records_payload(path)
    assert len(records) == 100, f"{path} must contain exactly 100 records"
    assert [record["id"] for record in records] and len({record["id"] for record in records}) == 100
    expected_by_id = {item["id"]: item for item in manifest}
    assert {record["id"] for record in records} == set(range(1, 101))

    parsed: list[ResearchRecord] = []
    for raw in records:
        parsed_record = ResearchRecord.model_validate(raw)
        expected = expected_by_id[parsed_record.id]
        assert parsed_record.name == expected["name"]
        assert parsed_record.category == expected["category"]
        assert parsed_record.website_hint == expected["website_hint"]
        _assert_evidence_contract(parsed_record)
        parsed.append(parsed_record)
    assert Counter(record.category for record in parsed) == Counter(
        {category: 10 for category in EXPECTED_CATEGORIES}
    )
    return parsed


def _artifact_or_skip(relative_path: str) -> Path:
    path = ROOT / relative_path
    if not path.exists():
        pytest.skip(f"staged artifact not present yet: {relative_path}")
    return path


def test_manifest_is_exactly_100_unique_apps_in_10_categories() -> None:
    manifest = _manifest()
    assert len(manifest) == 100
    assert {item["id"] for item in manifest} == set(range(1, 101))
    assert len({item["id"] for item in manifest}) == 100
    assert set(Counter(item["category"] for item in manifest)) == EXPECTED_CATEGORIES
    assert Counter(item["category"] for item in manifest) == Counter(
        {category: 10 for category in EXPECTED_CATEGORIES}
    )
    for item in manifest:
        AppInput.model_validate(item)


def test_fixed_pilot_and_holdout_ids_are_exact_and_disjoint() -> None:
    ids = {item["id"] for item in _manifest()}
    assert PILOT_IDS == (1, 11, 21, 31, 41, 51, 61, 71, 81, 91)
    assert HOLDOUT_IDS == (2, 12, 22, 32, 42, 52, 62, 72, 82, 92)
    assert set(PILOT_IDS) <= ids
    assert set(HOLDOUT_IDS) <= ids
    assert set(PILOT_IDS).isdisjoint(HOLDOUT_IDS)
    assert len({item["category"] for item in _manifest() if item["id"] in PILOT_IDS}) == 10
    assert len({item["category"] for item in _manifest() if item["id"] in HOLDOUT_IDS}) == 10


def test_valid_record_fixture_passes_schema_and_evidence_contract() -> None:
    record = ResearchRecord.model_validate(_valid_record_data())
    _assert_evidence_contract(record)


def test_schema_rejects_missing_confidence_and_invalid_mcp_url() -> None:
    missing_confidence = _valid_record_data()
    del missing_confidence["confidence"]["mcp"]
    with pytest.raises(ValidationError):
        ResearchRecord.model_validate(missing_confidence)

    missing_mcp_url = _valid_record_data()
    missing_mcp_url["mcp"] = {
        "status": "community",
        "notes": "Community server found.",
    }
    with pytest.raises(ValidationError):
        ResearchRecord.model_validate(missing_mcp_url)


def test_evidence_contract_rejects_confident_claim_without_support() -> None:
    data = _valid_record_data()
    data["evidence"] = [item for item in data["evidence"] if item["field"] != "auth_methods"]
    record = ResearchRecord.model_validate(data)
    with pytest.raises(AssertionError, match="auth_methods"):
        _assert_evidence_contract(record)


def test_evidence_contract_rejects_non_first_party_normal_source() -> None:
    data = _valid_record_data()
    auth_evidence = next(item for item in data["evidence"] if item["field"] == "auth_methods")
    auth_evidence["official_source"] = False
    auth_evidence["source_type"] = "community_repository"
    record = ResearchRecord.model_validate(data)
    with pytest.raises(AssertionError):
        _assert_evidence_contract(record)


@pytest.mark.parametrize("artifact_name", ["artifacts/v1.json", "artifacts/final.json"])
def test_staged_record_artifact_has_100_schema_valid_records(artifact_name: str) -> None:
    _assert_record_set(_artifact_or_skip(artifact_name), _manifest())


def _verification_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("results", "verifications", "urls"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            items = []
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def test_staged_verification_covers_every_final_evidence_url() -> None:
    final_path = _artifact_or_skip("artifacts/final.json")
    verification_path = _artifact_or_skip("artifacts/verification.json")
    records = _assert_record_set(final_path, _manifest())
    with verification_path.open(encoding="utf-8") as handle:
        items = _verification_items(json.load(handle))
    assert items, "verification.json must contain URL verification results"
    verified_urls: set[str] = set()
    for item in items:
        url = item.get("url") or item.get("requested_url") or item.get("final_url")
        assert url, "each verification result must identify a URL"
        verified_urls.add(str(url).rstrip("/"))
        assert item.get("http_status", item.get("status_code", 200))
    evidence_urls = {str(evidence.url).rstrip("/") for record in records for evidence in record.evidence}
    assert evidence_urls <= verified_urls


def test_staged_independent_browser_review_covers_fixed_holdout() -> None:
    verification_path = _artifact_or_skip("artifacts/verification.json")
    final_records = _assert_record_set(
        _artifact_or_skip("artifacts/final.json"), _manifest()
    )
    with verification_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert isinstance(payload, dict)
    rows = payload.get("independent_browser_review")
    assert isinstance(rows, list)
    expected = {(app_id, field) for app_id in HOLDOUT_IDS for field in HUMAN_FIELDS}
    actual = {(row.get("app_id"), row.get("field")) for row in rows}
    assert len(rows) == 70
    assert actual == expected
    final_by_id = {record.id: record for record in final_records}
    allowed = {"supported", "not_visible", "load_failed", "unresolved"}
    for row in rows:
        assert row.get("visible_support") in allowed
        if row["visible_support"] == "unresolved":
            assert row.get("url") is None
            assert _is_unknown(_field_value(final_by_id[row["app_id"]], row["field"]))
        else:
            assert str(row.get("url") or "").startswith(("http://", "https://"))
        if row["visible_support"] == "supported":
            assert row.get("browser_loaded") is True


def test_staged_human_review_sheet_has_exact_holdout_and_seven_fields() -> None:
    path = _artifact_or_skip("artifacts/human_review.csv")
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames is not None
        assert set(reader.fieldnames) == HUMAN_REVIEW_COLUMNS
        rows = list(reader)
    assert len(rows) == 70
    assert {int(row["app_id"]) for row in rows} == set(HOLDOUT_IDS)
    assert {row["field"] for row in rows} == HUMAN_FIELDS
    assert Counter(int(row["app_id"]) for row in rows) == Counter({app_id: 7 for app_id in HOLDOUT_IDS})
    manifest_by_id = {item["id"]: item for item in _manifest()}
    for row in rows:
        expected_name = manifest_by_id[int(row["app_id"])]
        assert row["app_name"] == expected_name["name"]
        assert row["official_evidence_url"].startswith(("http://", "https://"))
        assert row["human_v1_result"] in ALLOWED_HUMAN_RESULTS
        assert row["human_final_result"] in ALLOWED_HUMAN_RESULTS


def test_metrics_cannot_claim_final_accuracy_before_completed_human_review() -> None:
    metrics_path = ROOT / "artifacts" / "metrics.json"
    if not metrics_path.exists():
        pytest.skip("metrics artifact not present yet")
    review_path = ROOT / "artifacts" / "human_review.csv"
    assert review_path.exists(), "metrics cannot be staged without the human-review sheet"
    with review_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    review_complete = bool(rows) and all(
        row["human_v1_result"] in {"correct", "incorrect", "unclear"}
        and row["human_final_result"] in {"correct", "incorrect", "unclear"}
        for row in rows
    )
    with metrics_path.open(encoding="utf-8") as handle:
        metrics = json.load(handle)
    assert isinstance(metrics, dict)
    total_keys = {"total_apps", "app_count", "record_count", "total_records"}
    present_totals = [metrics[key] for key in total_keys if key in metrics]
    assert present_totals and all(value == 100 for value in present_totals)

    accuracy_values = {
        key: value
        for key, value in metrics.items()
        if "accuracy" in key.lower()
    }
    if not review_complete:
        assert all(value in (None, "", "pending") for value in accuracy_values.values()), (
            "accuracy must remain pending until all human-result cells are completed"
        )


def _content_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", ".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.name == ".env" or path.suffix.lower() not in {".py", ".json", ".jsonl", ".csv", ".html", ".md", ".txt"}:
            continue
        files.append(path)
    return files


def test_tracked_and_generated_content_has_no_secret_values() -> None:
    secret_patterns = [
        re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}"),
        re.compile(r"\b(?:ghp|github_pat|xox[ab])-[A-Za-z0-9_-]{20,}"),
        re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"\bBearer\s+[A-Za-z0-9._-]{24,}"),
        re.compile(r"(?:OPENAI|COMPOSIO)_API_KEY\s*=\s*['\"]?[^'\"\s]+"),
    ]
    violations: list[str] = []
    for path in _content_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in secret_patterns:
            if pattern.search(text):
                violations.append(str(path.relative_to(ROOT)))
                break
    assert not violations, f"possible secret value found in: {sorted(set(violations))}"


def test_generated_html_uses_tailwind_v4_light_theme_rules() -> None:
    path = ROOT / "site" / "index.html"
    if not path.exists():
        pytest.skip("site/index.html not generated yet")
    html = path.read_text(encoding="utf-8")
    assert re.search(r"https://cdn\.jsdelivr\.net/npm/@tailwindcss/browser@4(?:[\"'/?]|$)", html)
    assert "dark:" not in html
    assert not re.search(r"(?<![\w-])font-bold(?![\w-])", html)
    assert "API breadth / style" in html
    assert 'id="browser-proof"' in html
