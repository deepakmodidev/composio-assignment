# Composio 100-app research agent

This repository is the runnable solution to the [assignment](doc/ASSIGNMENT.md). It researches the fixed 100-app set with an evidence-first loop, preserves the first pass, independently verifies every submitted citation, and generates a single-page case study from the resulting artifacts.

- [Live case study](https://deepakmodidev.github.io/composio-assignment/)
- [Source repository](https://github.com/deepakmodidev/composio-assignment)

The agent never treats model memory or a search snippet as evidence. A factual value survives only when the pipeline fetches an allowed source, stores a supporting passage, and a fresh critic confirms that the passage supports the mapped claim. Otherwise the value becomes `unknown` or `none_found`.

## What the agent does

For each app, the loop:

1. Calls Composio's session-backed MCP `COMPOSIO_SEARCH_TOOLS` capability to check live Composio toolkit/tool coverage.
2. Uses OpenAI web search to discover current vendor-controlled documentation.
3. Downloads those pages directly, using Playwright only when normal HTTP cannot render them.
4. Extracts the seven assignment fields as strict structured output with claim-level evidence.
5. Checks source ownership, HTTP resolution, and whether every stored passage exists on the fetched page.
6. Runs a fresh-context model critic and retries only failed fields, up to three total attempts.
7. Saves the record immediately so an interrupted 100-app run can resume.

Composio catalog coverage is intentionally separate from an app's own MCP status. The former proves whether Composio currently exposes tools for the app; the latter is researched from the app vendor's documentation or an explicitly labeled original community repository.

## Setup

Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) are required.

```bash
cp .env.example .env
# Add OPENAI_API_KEY and COMPOSIO_API_KEY to .env
uv sync
uv run playwright install chromium
uv run pytest -q
```

The default August 2026 model split is `gpt-5.6-terra` for discovery/extraction and `gpt-5.6-sol` for the independent critic. Override `RESEARCH_MODEL` or `VERIFY_MODEL` in `.env` if needed.

## Run

Research one app while developing:

```bash
uv run python -m research_agent.pipeline --app 1 --concurrency 1
```

Run the fixed cross-category pilot and its independent verification:

```bash
uv run python -m research_agent.pipeline --pilot
uv run python -m research_agent.verify --pilot
```

After the pilot is calibrated, process the same loop across the other 90 and freeze V1:

```bash
uv run python -m research_agent.pipeline --remaining --concurrency 5
uv run python -m research_agent.pipeline --freeze-v1
```

Re-fetch all V1 citations, correct unsupported fields without changing V1, and create the final dataset:

```bash
uv run python -m research_agent.verify --all --max-attempts 3 --concurrency 5
uv run python -m research_agent.browser_review
uv run python -m research_agent.report --prepare-review
uv run pytest -q
```

Valid per-app files are reused automatically. Pass `--force` to the pipeline only when deliberately rerunning a calibration record. V1 refuses to overwrite an existing, different frozen dataset.

## Evidence policy

Normal claims accept only official developer documentation, API references, help/onboarding/pricing pages, product pages, or repositories owned by the app's official organization. A community MCP may use its original public repository, is labeled `community`, and is never presented as first-party evidence. Aggregators, directories, blogs, forums, search snippets, and unsupported inference are rejected.

`none_found` means the documented search found no supported result; it does not prove nonexistence. `unknown` means the fetched evidence was insufficient or contradictory. The pipeline records both outcomes instead of guessing.

## Human checkpoint

`--prepare-review` creates `artifacts/human_review.csv` with the fixed ten-app holdout and seven fields per app (70 rows). Values and source links are already populated. The applicant opens each link and fills only:

- `human_v1_result`: `correct`, `incorrect`, or `unclear`
- `human_final_result`: `correct`, `incorrect`, or `unclear`
- `human_correction` and `human_notes` when useful

The report command deliberately refuses to claim final accuracy until both result columns are complete for all 70 rows:

```bash
uv run python -m research_agent.report --final
uv run pytest -q
```

The automated/fresh-agent/browser checks are recorded separately and are never labeled as human review.

## Outputs

- `artifacts/apps/`: resumable per-app records
- `artifacts/v1.json`: immutable first-pass 100-app dataset
- `artifacts/final.json`: corrected dataset
- `artifacts/verification.json`: URL checks, field audits, and correction history
- `artifacts/human_review.csv`: genuine applicant-review sheet
- `artifacts/metrics.json`: all displayed counts and accuracy values
- `artifacts/run_log.jsonl`: secret-safe execution events
- `site/index.html`: one light-theme Tailwind browser-CDN case study

Serve the page locally with:

```bash
python -m http.server 8000 --directory site
```

Then open `http://localhost:8000`. The HTML has no frontend build step; network access is required to load Tailwind's browser CDN.

## Known limitations

- JavaScript-heavy, bot-protected, or login-gated documentation can defeat both HTTP and browser retrieval. Those fields remain visible as unknowns.
- Public documentation can establish a gate but cannot test paid credentials for the 100 apps.
- Community MCP discovery cannot prove that no unpublished implementation exists.
- Paid credentials were not used to execute actions against the researched apps; the study evaluates their documented integration paths.
