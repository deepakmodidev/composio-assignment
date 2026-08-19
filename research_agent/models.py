from __future__ import annotations

from enum import StrEnum
from typing import Literal

from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAJOR_FIELDS = (
    "description",
    "auth_methods",
    "credential_access",
    "api_styles",
    "api_breadth",
    "mcp",
    "buildability",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def validate_http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must use http or https")
    return value


class AuthMethod(StrEnum):
    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    BASIC = "basic"
    TOKEN = "token"
    OTHER = "other"
    UNKNOWN = "unknown"


class AccessStatus(StrEnum):
    FREE = "free"
    FREE_TRIAL = "free_trial"
    PAID = "paid"
    ADMIN_GATED = "admin_gated"
    PARTNER_GATED = "partner_gated"
    UNKNOWN = "unknown"


class ApiBreadth(StrEnum):
    BROAD = "broad"
    MODERATE = "moderate"
    NARROW = "narrow"
    NONE_FOUND = "none_found"
    UNKNOWN = "unknown"


class McpStatus(StrEnum):
    OFFICIAL = "official"
    COMMUNITY = "community"
    NONE_FOUND = "none_found"
    UNKNOWN = "unknown"


class BuildabilityVerdict(StrEnum):
    READY = "ready"
    CONDITIONAL = "conditional"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AppInput(StrictModel):
    id: int = Field(ge=1, le=100)
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    website_hint: str = Field(min_length=1)


class DiscoveredSource(StrictModel):
    field: str
    url: str
    source_type: Literal[
        "official_docs",
        "official_api_reference",
        "official_help",
        "official_product",
        "official_repository",
        "community_repository",
    ]
    rationale: str

    _validate_url = field_validator("url")(validate_http_url)


class ComposioCoverage(StrictModel):
    searched: bool
    toolkit_found: bool
    toolkit_slug: str | None = None
    relevant_tools: list[str] = Field(default_factory=list)
    notes: str = ""


class SourceDiscovery(StrictModel):
    official_sources: list[DiscoveredSource]
    composio_coverage: ComposioCoverage
    unresolved_fields: list[str] = Field(default_factory=list)


class FetchedPage(StrictModel):
    requested_url: str
    final_url: str
    page_title: str
    text: str
    fetched_at: str
    http_status: int
    method: Literal["http", "browser"]
    source_type: Literal[
        "official_docs",
        "official_api_reference",
        "official_help",
        "official_product",
        "official_repository",
        "community_repository",
    ]
    official_source: bool

    _validate_requested_url = field_validator("requested_url")(validate_http_url)
    _validate_final_url = field_validator("final_url")(validate_http_url)


class Evidence(StrictModel):
    field: Literal[
        "description",
        "auth_methods",
        "credential_access",
        "api_styles",
        "api_breadth",
        "mcp",
        "buildability",
    ]
    url: str
    page_title: str
    supporting_text: str = Field(min_length=1, max_length=700)
    source_type: Literal[
        "official_docs",
        "official_api_reference",
        "official_help",
        "official_product",
        "official_repository",
        "community_repository",
    ]
    fetched_at: str
    http_status: int
    official_source: bool
    claim_supported: bool

    _validate_url = field_validator("url")(validate_http_url)


class CredentialAccess(StrictModel):
    status: AccessStatus
    requirements: str


class ApiSurface(StrictModel):
    styles: list[str]
    breadth: ApiBreadth
    notes: str


class McpSurface(StrictModel):
    status: McpStatus
    url: str | None = None
    notes: str

    @model_validator(mode="after")
    def require_url_when_found(self) -> "McpSurface":
        if self.status in {McpStatus.OFFICIAL, McpStatus.COMMUNITY} and self.url is None:
            raise ValueError("MCP URL is required when an MCP implementation is found")
        if self.url is not None:
            validate_http_url(self.url)
        return self


class Buildability(StrictModel):
    verdict: BuildabilityVerdict
    reason: str
    main_blocker: str


class ConfidenceMap(StrictModel):
    description: Confidence
    auth_methods: Confidence
    credential_access: Confidence
    api_styles: Confidence
    api_breadth: Confidence
    mcp: Confidence
    buildability: Confidence


class ExtractedResearch(StrictModel):
    description: str
    auth_methods: list[AuthMethod]
    credential_access: CredentialAccess
    api_surface: ApiSurface
    mcp: McpSurface
    buildability: Buildability
    confidence: ConfidenceMap
    evidence: list[Evidence]
    unresolved_issues: list[str] = Field(default_factory=list)

class ResearchRecord(ExtractedResearch):
    id: int = Field(ge=1, le=100)
    name: str
    category: str
    website_hint: str
    composio_coverage: ComposioCoverage
    attempt_count: int = Field(ge=1, le=3)
    status: Literal["complete", "partial"]


class FieldAudit(StrictModel):
    field: Literal[
        "description",
        "auth_methods",
        "credential_access",
        "api_styles",
        "api_breadth",
        "mcp",
        "buildability",
    ]
    supported: bool
    explanation: str


class RecordAudit(StrictModel):
    fields: list[FieldAudit]
    failed_fields: list[str]
    all_confident_claims_supported: bool
