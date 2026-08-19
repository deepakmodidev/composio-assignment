# Exact Build Plan: Composio 100-App Research Assignment

## Objective

Build a clean, minimal research agent that completes `ASSIGNMENT.md`:

1. Calibrate the automation on 10 fixed pilot apps.
2. Run the same automation on the remaining 90 apps.
3. Fetch and verify evidence from first-party documentation.
4. Preserve V1 and corrected results.
5. Prepare one fixed 10-app human audit.
6. Compute patterns and accuracy from the data.
7. Generate one clean single-file HTML case study with Tailwind Browser CDN and a concise README.

This is an assignment solution, not a production platform. Do not add a database, backend service, React application, authentication system, or unnecessary abstractions.

## Required inputs

- `ASSIGNMENT.md`: requirements and app list.
- `.env`: contains `OPENAI_API_KEY` and `COMPOSIO_API_KEY`.

Secret rules:

- Never print, log, embed, or commit secret values.
- Add `.env` to `.gitignore` before the first commit.
- Add an `.env.example` containing empty variable names only.
- Do not obtain credentials for the 100 apps; research their public documentation.

## Minimal project structure

```text
.
├── ASSIGNMENT.md
├── BUILD_PLAN.md
├── GOAL_PROMPT.md
├── README.md
├── .env
├── .env.example
├── .gitignore
├── pyproject.toml
├── data/
│   └── apps.json
├── artifacts/
│   ├── apps/
│   ├── v1.json
│   ├── final.json
│   ├── verification.json
│   ├── human_review.csv
│   ├── metrics.json
│   └── run_log.jsonl
├── research_agent/
│   ├── models.py
│   ├── pipeline.py
│   ├── sources.py
│   ├── verify.py
│   └── report.py
├── site/
│   └── index.html
└── tests/
    └── test_pipeline.py
```

Responsibilities:

- `models.py`: Pydantic schemas and enums.
- `pipeline.py`: Composio research, extraction, retry, checkpoint, and resume loop.
- `sources.py`: official-domain policy, HTTP fetching, browser fallback, and passage checks.
- `verify.py`: URL, source ownership, claim support, independent agent, and browser checks.
- `report.py`: human-review sheet, accuracy, aggregate findings, and HTML generation.

Use Python 3.11+ and `uv`. Keep functions short, names explicit, and dependencies minimal.

## Fixed 10-app pilot

Use exactly one app from each category:

| ID | App |
|---:|---|
| 1 | Salesforce |
| 11 | Zendesk |
| 21 | Slack |
| 31 | Google Ads |
| 41 | Shopify |
| 51 | DataForSEO |
| 61 | GitHub |
| 71 | Notion |
| 81 | Stripe |
| 91 | NotebookLM |

The pilot passes only when all 10 records:

- Pass the schema.
- Contain fetched evidence for every confident major claim.
- Follow the source policy.
- Have verified evidence URLs.
- Use `unknown` for unsupported claims.

Fix shared pipeline or prompt problems found during the pilot. Then rerun the pilot once with the final calibrated pipeline before processing the remaining 90.

## Fixed 10-app human holdout

Use exactly one non-pilot app from each category:

| ID | App |
|---:|---|
| 2 | HubSpot |
| 12 | Intercom |
| 22 | Twilio |
| 32 | Meta Ads |
| 42 | WooCommerce |
| 52 | SE Ranking |
| 62 | Vercel |
| 72 | Airtable |
| 82 | Plaid |
| 92 | Otter AI |

This sample is fixed before results exist to avoid cherry-picking.

## Required record schema

Each app must include:

- `id`, `name`, `category`, and `website_hint`.
- One-line `description`.
- `auth_methods`.
- `credential_access.status` and `credential_access.requirements`.
- `api_surface.styles`, `api_surface.breadth`, and notes.
- `mcp.status`, URL when found, and notes.
- `buildability.verdict`, reason, and main blocker.
- Confidence for every researched field.
- Claim-level evidence.
- Attempt count, status, and unresolved issues.

Allowed values:

- Auth: `oauth2`, `api_key`, `basic`, `token`, `other`, `unknown`.
- Access: `free`, `free_trial`, `paid`, `admin_gated`, `partner_gated`, `unknown`.
- API breadth: `broad`, `moderate`, `narrow`, `none_found`, `unknown`.
- MCP: `official`, `community`, `none_found`, `unknown`.
- Buildability: `ready`, `conditional`, `blocked`, `unknown`.
- Confidence: `high`, `medium`, `low`.

Each evidence item must contain:

```json
{
  "field": "auth_methods",
  "url": "https://official.example/docs/auth",
  "page_title": "Authentication",
  "supporting_text": "Short text fetched from this page",
  "source_type": "official_docs",
  "fetched_at": "ISO-8601 timestamp",
  "http_status": 200,
  "official_source": true,
  "claim_supported": true
}
```

Meaning:

- `none_found` means the documented search did not locate evidence.
- `unknown` means the available evidence was insufficient or contradictory.
- Neither value means proven nonexistence.

## Source contract

Search is only a discovery mechanism. Search snippets and model memory are never evidence.

Accepted first-party evidence for normal app claims:

1. Official developer documentation.
2. Official API reference.
3. Official authentication documentation.
4. Official developer onboarding, pricing, plan, or help page.
5. Official product website.
6. Repository owned by the app's official organization.

MCP exception:

- Official MCP: official documentation or official repository.
- Community MCP: original public repository, explicitly labeled `community`.
- Directories, listicles, and articles are not MCP evidence.

Required mapping:

| Claim | Required evidence |
|---|---|
| Description | Official product page or official docs |
| Authentication | Official auth guide or API reference |
| Credential access | Official onboarding, pricing, plan, or help page |
| API style and breadth | Official API docs/reference |
| Official MCP | Official docs/repository |
| Community MCP | Original repository, labeled community |
| Buildability blocker | Official evidence supporting the limitation |

Forbidden evidence:

- Model memory
- Search-result snippets
- AI summaries
- Aggregators
- SEO articles
- Comparison blogs
- Forum answers
- Unsupported inference

If the required source is unavailable, output `unknown` or `none_found`. Do not lower the evidence standard.

## Autonomous research loop

For each selected app:

1. Load its assignment metadata and known official domain.
2. Query Composio's session-backed MCP catalog for live toolkit/tool coverage for the app.
3. Use OpenAI web search to discover candidate first-party pages, then fetch each candidate page.
4. Use browser automation only when ordinary fetching cannot render the content.
5. Extract the required fields as structured output.
6. Attach a short fetched passage to every confident claim.
7. Validate the record.
8. Verify it in a fresh model context.
9. Retry only failed fields using targeted queries.
10. Stop after three total attempts.
11. Convert still-unsupported fields to `unknown` or `none_found`.
12. Save the app immediately so the run can resume.

```python
for app in selected_apps:
    if verified_record_exists(app) and not force:
        continue

    failed_fields = required_fields

    for attempt in range(1, 4):
        composio_coverage = inspect_composio_catalog(app)
        sources = discover_official_sources(app, failed_fields)
        pages = fetch_official_sources(sources)
        candidate = extract_structured_record(app, pages, composio_coverage)
        audit = verify_in_fresh_context(candidate, pages)
        save_attempt(app, candidate, audit)

        if audit.all_confident_claims_supported:
            save_verified_record(candidate)
            break

        failed_fields = audit.failed_fields
    else:
        save_with_unknowns(candidate, failed_fields)
```

Runtime rules:

- Maximum five apps concurrently.
- Retry `429` and `5xx` responses with bounded exponential backoff.
- Resume from valid per-app files.
- Never overwrite a valid completed record unless `--force` is supplied.
- Log app ID, phase, attempt, tool name, status, and timestamp.
- Never log environment values or secrets.

## Exact execution sequence

### 1. Scaffold and test

```bash
uv sync
uv run pytest -q
```

### 2. Calibrate the pilot

```bash
uv run python -m research_agent.pipeline --pilot
uv run python -m research_agent.verify --pilot
```

If pilot validation fails, correct only the shared prompt, schema, or pipeline logic. Rerun the full pilot. Do not hand-edit research answers.

### 3. Process the remaining 90

```bash
uv run python -m research_agent.pipeline --remaining --concurrency 5
```

### 4. Freeze V1

```bash
uv run python -m research_agent.pipeline --freeze-v1
```

`artifacts/v1.json` must now contain exactly 100 unique apps. It must never be modified after this point.

### 5. Verify and correct all 100

```bash
uv run python -m research_agent.verify --all --max-attempts 3
```

This must:

- Re-fetch every evidence URL.
- Record status and final URL.
- Verify first-party ownership.
- Verify that fetched content supports the mapped claim.
- Use browser automation when required.
- Run an independent agent critic.
- Retry unsupported fields.
- Produce `artifacts/verification.json`.
- Produce `artifacts/final.json` without changing V1.

### 6. Prepare report and human review

```bash
uv run python -m research_agent.report --prepare-review
```

This creates:

- `artifacts/metrics.json`.
- `artifacts/human_review.csv` for the fixed 10-app holdout.
- A draft `site/index.html` marked `Human review pending`.

### 7. Human review

Before the applicant check, Codex must perform a separate manual browser inspection of the fixed holdout and record it as `independent_browser_review`. This is an additional verification layer, not a human review.

The applicant then opens the prepared official links and reviews seven fields for each holdout app:

1. Description
2. Authentication
3. Credential access
4. API style
5. API breadth
6. MCP
7. Buildability/blocker

The CSV must include:

```text
app_id,app_name,field,v1_value,final_value,official_evidence_url,
human_v1_result,human_final_result,human_correction,human_notes
```

Allowed human results:

- `correct`
- `incorrect`
- `unclear`

The applicant does not research from scratch; the sheet already contains the values, evidence links, and supporting text. Codex must not fill the human-result columns or claim its own checks are human checks.

### 8. Finalize after human review

```bash
uv run python -m research_agent.report --final
uv run pytest -q
```

The final report command must refuse to complete while any human-result field is empty.

## Accuracy and patterns

Compute all metrics from files; never type totals manually.

Verification metrics:

- Citation resolution rate.
- Claim-support rate.
- First-party evidence rate.
- Unresolved-field rate.
- V1 field accuracy on the human holdout.
- Final field accuracy on the same holdout.
- Corrections made between V1 and final.

Accuracy:

```text
accuracy = fields marked correct / all reviewed fields
```

Report `unclear` separately and explain how it affects the denominator.

Pattern analysis:

- Auth-method distribution.
- Self-serve versus gated access.
- Access by category.
- Buildability by category.
- Most common blockers.
- Official/community MCP counts.
- Easy wins: `ready` plus `free` or `free_trial`.
- Outreach candidates: useful API plus `admin_gated` or `partner_gated`.
- Unknown and low-confidence cases.

Generate the headline only after these results exist.

## Buildability rubric

- `ready`: useful documented API, obtainable credentials, and no material blocker.
- `conditional`: buildable but requires payment, admin setup, unusual setup, or has a narrow API.
- `blocked`: official evidence proves partnership approval, unavailable credentials, or no usable public integration path.
- `unknown`: evidence is insufficient.

Do not mark an app `blocked` merely because docs were not found.

## HTML requirements

`site/index.html` must be one HTML file using Tailwind CSS v4 through the official browser CDN and small, readable vanilla JavaScript:

```html
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
```

This take-home page intentionally uses the browser CDN to avoid a frontend build system. Document that it requires network access to load Tailwind.

The page must contain:

1. Result-driven headline.
2. Four to six computed metrics.
3. Auth and access patterns.
4. Easy-wins versus outreach matrix.
5. Category comparison.
6. Searchable/filterable 100-app table.
7. Evidence links.
8. Agent workflow and proof of Composio usage.
9. V1 versus final accuracy.
10. Honest corrections, failures, and unknowns.
11. Reproduction commands and repository/live links.

Rules:

- No manually entered totals.
- No fabricated accuracy.
- No claim of human verification until the CSV is complete.
- Tailwind Browser CDN is the only UI dependency; do not add React, Vue, component libraries, or a frontend build system.
- Use a light theme only. Do not add dark mode, `dark:` classes, theme switching, near-black page backgrounds, or color-scheme detection.
- Do not use the `font-bold` utility. Use `font-medium` or `font-semibold` only when hierarchy needs emphasis.
- Responsive on desktop and mobile.
- No browser console errors.

## README requirements

Include:

- What the agent does.
- How Composio SDK/MCP is actually used.
- Setup commands.
- One-app, pilot, remaining, verification, and report commands.
- Retry and resume behavior.
- Evidence policy.
- Human-review instructions.
- Known limitations and unresolved apps.
- Local page and deployment instructions.

## Automated acceptance tests

Tests must fail unless:

- The manifest has exactly 100 unique IDs and 10 categories.
- Pilot IDs are exactly `1,11,21,31,41,51,61,71,81,91`.
- Holdout IDs are exactly `2,12,22,32,42,52,62,72,82,92`.
- V1 and final each contain exactly 100 unique records.
- Every record passes the schema.
- Every confident major claim has mapped evidence.
- Every normal evidence source is first-party.
- Every community MCP is explicitly labeled.
- Every submitted URL has a verification result.
- Unsupported fields use `unknown` or `none_found`.
- Totals reconcile to 100.
- No secrets exist in generated/tracked content.
- HTML passes a browser smoke test.
- Final accuracy appears only after genuine human review.

## Stop conditions

The autonomous phase is done only when:

- The 10-app pilot has passed.
- The remaining 90 have run through the same pipeline.
- V1 is frozen with 100 records.
- Automated verification has covered all 100 records and all citations.
- Final corrected data, metrics, draft HTML, README, logs, and the human-review sheet exist.
- Tests for the autonomous phase pass.

At that point, pause once for the genuine human review. Do not call the whole assignment complete.

After the applicant completes the review sheet and resumes the goal, finish only when:

- Human-review fields are complete.
- V1 and final accuracy are computed.
- Final HTML is rebuilt and smoke-tested.
- All tests pass.
- No secrets are present.
- Repository and live-page links are real.

Valid external blockers:

1. Invalid required API credentials.
2. Repeated network/provider failure after bounded retries.
3. Pending genuine human review.
4. Missing Git or hosting authorization.

For a blocker, preserve all progress and state the exact next action. Never invent a deployment or mark incomplete work complete.

## Deployment

Deployment happens only after human review and tests pass.

If Git/hosting access is available, deploy the static `site/index.html` and record the real URL. If access is unavailable, leave the site deploy-ready and report the missing authorization. Never invent a live link.
