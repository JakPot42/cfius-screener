# CFIUS Screener — Claude Code context

FastAPI web app that screens foreign-investment transactions for CFIUS
jurisdiction and mandatory-declaration triggers under 31 CFR Part 800,
with a full cited findings trail for every determination.

## Tech stack

- Python 3.11.9 (Render) / 3.14 (local, via `py`)
- FastAPI + Jinja2 templates + vanilla CSS (no CDN)
- SQLAlchemy 2.0 + SQLite (ephemeral on Render free tier — seed runs every cold start)
- Claude Haiku from Milestone 2 (intake parsing + memo narrative only)

## Milestone status

- **Milestone 1 ✅** — deterministic Part 800 decision tree, structured-fact
  form, findings trail with citations, 3 seeded demo scenarios, test suite
- **Milestone 2 ⬜** — Claude intake (parse → human confirm), TID classifier
  assist, memo narrative, ReportLab PDF
- **Milestone 3 ⬜** — OFAC SDN screen (port GhostTrace `ofac_checker.py`),
  threat/vulnerability/consequence risk scoring

## Architecture decisions

**The law is deterministic code; Claude never makes a legal determination.**
`jurisdiction_engine.py` is pure functions — no DB, no web, no Claude.
Mandatory-filing status carries civil penalties up to the transaction value;
an LLM must never decide it. Claude (M2) parses deal descriptions into
`TransactionFacts` (human-confirmed before running) and writes the memo
narrative about the engine's conclusions.

**Every regulatory number lives in `config.py` with a citation and a VERIFY
marker.** Encoded from public 31 CFR Part 800 text, unverified by counsel.
The engine imports values; it never defines its own.

**The findings trail is the product.** Every tree node emits a Finding
(question / answer / determination / citation) and the result page renders
all of them, including for NOT_COVERED early exits.

**Excepted-investor nuance (easy to get wrong):** excepted investors
(AU/CA/NZ/UK, simplified country test) are carved out of covered-INVESTMENT
jurisdiction and ALL mandatory declarations — but covered CONTROL
transactions remain reviewable. A Canadian 100% buyout is still covered;
the filing is just voluntary.

**Substantial-interest mandatory prong needs all three:** foreign government
≥49% of acquirer AND acquirer takes ≥25% voting AND target is TID. Both
thresholds inclusive. A non-controlling 10% stake never triggers it
regardless of state ownership.

**`screening_service.run_and_store()` is the single path** from facts to
stored row — the web form and the seeder both use it.

**Starlette 1.x TemplateResponse signature:** `TemplateResponse(request, name, ctx)`.

**Anthropic exception pattern (M2):** catch `Exception`, not
`anthropic.APIError` — the SDK raises `TypeError` on auth failures.

## Module map

| File | Purpose |
|---|---|
| `main.py` | FastAPI app + routes |
| `config.py` | All regulatory parameters, citations, thresholds — no logic |
| `jurisdiction_engine.py` | The deterministic Part 800 decision tree |
| `screening_service.py` | Engine ↔ DB glue, JSON (de)serialization helpers |
| `models.py` | SQLAlchemy ORM: Screening |
| `database.py` | Engine, Base, get_db, init_db |
| `seed_data.py` | 3 fictional demo scenarios (idempotent) |

## Test suite

Run with `py -m pytest`. `tests/conftest.py` sets `DATABASE_URL` to a
throwaway file DB before any import — keep that import-order trick in mind.
No test makes a network call.

## Deployment

- Render service ID: `srv-d8lgdkt7vvec739knr60`
- Live URL: https://cfius-screener.onrender.com
- GitHub: `JaKPoT-Sudo/cfius-screener`, auto-deploys on push to `master`
- Env vars set on the service (June 11, 2026): `PYTHON_VERSION=3.11.9`,
  `DEMO_MODE=True`, `ANTHROPIC_API_KEY` (unused until Milestone 2)
- render.yaml envVars only apply via Blueprints — set them in the dashboard
  or via the Render API for auto-deploys
