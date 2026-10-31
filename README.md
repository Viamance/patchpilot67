# PatchPilot

From a failing test to an AI-proposed, sandbox-verified patch, with the evidence visible.

PatchPilot runs one complete repair cycle on a prepared Python fixture: **Daytona** reproduces the failing tests in an isolated sandbox, a model served by **Nosana** proposes a small structured fix, PatchPilot validates and applies it inside the sandbox, and Daytona reruns the same unchanged tests. The verdict comes from the test results, never from the model.

The full MVP spec is in [mvp.md](mvp.md).

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1    macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:

- `DAYTONA_API_KEY`: from app.daytona.io/dashboard/keys
- `NOSANA_API_KEY`: from deploy.nosana.com, needs `inference:use` permission and usable credits
- `DAYTONA_TARGET` (optional): `us` (default) or `eu`

Never commit `.env`.

## Run

```bash
python -m streamlit run app.py
```

Pick a currently served Nosana model in the sidebar, then click **Repair demo bug**.

## How it works

1. A fresh Daytona sandbox receives `fixture/pricing.py` and `fixture/test_pricing.py`.
2. Baseline: `python -m pytest -q`. Exit 0 stops with **NO FAILURE REPRODUCED**; any code other than 1 is an **ERROR**.
3. One Nosana request returns JSON with `diagnosis`, `explanation` and one edit (`path`, `old`, `new`).
4. The edit is rejected unless it targets `pricing.py` only, is non-empty, and its `old` snippet matches exactly once. Tests are never modified, and no model-supplied command is ever run.
5. The edit is applied inside the sandbox, the original tests are re-uploaded, and the same command reruns.
6. Exit 0 is **VERIFIED**, exit 1 is **NOT VERIFIED**, anything else is **ERROR**. The sandbox is deleted on every path, and a cleanup failure is reported separately with the sandbox ID.

The local fixture file is only read, never written, so every run starts from the original bug.

**Seeded demo defect:** `apply_discount` returns the discount amount instead of the discounted price.

## Tests

```bash
python -m pytest -q
```

These offline tests cover proposal validation, verdict mapping and the full run flow against a local fake sandbox (real pytest, fake providers): a genuine repair, a non-fixing patch, an out-of-scope edit, a provider failure, an already-passing baseline, and cleanup. They test the logic only; they are not evidence of live Daytona or Nosana integration.

## Integration status

- Daytona and Nosana: implemented; live end-to-end run pending verification on the team laptop.
- Neo4j: deferred, not claimed (see mvp.md section 7).

VERIFIED means the designated tests passed after the patch; it does not prove arbitrary code is correct.
