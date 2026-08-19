# Ready-to-Paste Codex Goal

Copy the complete command below into Codex:

```text
/goal Implement the assignment in ASSIGNMENT.md exactly according to BUILD_PLAN.md.

Read ASSIGNMENT.md and BUILD_PLAN.md completely before changing files. Treat BUILD_PLAN.md as the execution contract.

Work autonomously through the defined checkpoints. Ask the applicant only for a material ambiguity, external authorization, or final human confirmation. Use simple Python, keep the code clean and easy to read, and do not add infrastructure or features not required by the assignment.

Non-negotiable requirements:

- Use the existing OPENAI_API_KEY and COMPOSIO_API_KEY without printing or committing them.
- Use Composio SDK with a session-backed MCP capability to inspect live Composio toolkit/tool coverage for every app. Use web search separately to discover first-party documentation.
- Calibrate on the exact 10 pilot apps, then run the same automation on the remaining 90.
- Do not manually author app research rows.
- Use search only to discover sources. Every confident app claim must be supported by fetched first-party documentation. Search snippets, model memory, aggregators, blogs, and unsupported inference are not evidence.
- Verify every evidence URL and its mapped claim. Retry failed fields at most three times, then use unknown or none_found.
- Preserve an untouched 100-app V1 dataset and a separate corrected final dataset.
- Run independent agent verification and browser verification as specified.
- Generate all totals, insights, and accuracy figures from artifacts rather than typing them manually.
- Perform a separate manual browser inspection of the fixed 10-app holdout and label it independent_browser_review, not human review.
- Generate the fixed 10-app human-review sheet, but never fill its human-result columns or claim your own checks are human checks.
- Generate the README, execution logs, tests, metrics, and single-file HTML case study required by the plan.
- Build site/index.html with the Tailwind CSS v4 browser CDN and small vanilla JavaScript only. Use a light theme, no dark mode or dark: classes, and never use the font-bold utility; use font-medium or font-semibold sparingly.
- Never expose secrets.
- Never fabricate evidence, accuracy, human verification, repository links, deployment links, or completion.

After each checkpoint, run its validation commands and append a short, secret-safe status entry to artifacts/run_log.jsonl. Fix failures and continue.

Autonomous stopping condition:

Stop for the single human checkpoint only after the pilot, remaining 90, frozen V1, automated verification, corrected final data, metrics, README, draft HTML, tests, and prepared human-review sheet all exist and pass the autonomous acceptance tests. Report exactly how the applicant completes the review sheet.

When the completed human-review sheet is provided and the goal is resumed, compute V1 and final accuracy, rebuild and browser-test the final HTML, rerun all tests, and deploy only if real Git/hosting authorization is available.

Do not mark the assignment complete until every Definition of Done item in BUILD_PLAN.md is satisfied. If an external blocker remains, preserve progress and report the exact missing input or authorization.
```

## What happens during the goal

1. Codex builds and tests the research loop.
2. The loop calibrates on 10 fixed apps.
3. The same loop researches the remaining 90.
4. Codex automatically verifies all sources and claims.
5. Codex builds the draft case study and prepared audit sheet.
6. Codex pauses once for the applicant's genuine 10-app review.
7. After `/goal resume`, Codex computes measured accuracy and finishes the page.
8. Deployment occurs only with real hosting authorization.

The human checkpoint is required by the assignment. Everything before and after it is autonomous.
