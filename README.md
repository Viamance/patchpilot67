# PatchPilot

From a failing test to an AI-proposed, sandbox-verified patch, with the evidence visible.

PatchPilot runs one complete repair cycle on a prepared Python fixture: **Daytona** reproduces the failing tests in an isolated sandbox, **Neo4j** maps the failing tests to the code they exercise, a model served by **Nosana** proposes a small structured fix, PatchPilot validates and applies it inside the sandbox, and Daytona reruns the same unchanged tests. The verdict comes from the test results, never from the model.

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
- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` (optional): an AuraDB Free instance works. Without them the repair runs without graph context.

Never commit `.env`.

## Run

```bash
python -m streamlit run app.py
```

Pick a currently served Nosana model in the sidebar, then click **Repair demo bug**.

## How it works

1. A fresh Daytona sandbox receives `fixture/pricing.py` and `fixture/test_pricing.py`.
2. Baseline: `python -m pytest -q`. Exit 0 stops with **NO FAILURE REPRODUCED**; any code other than 1 is an **ERROR**.
3. Neo4j (optional): the fixture's files, functions, imports and test targets are read with Python's `ast` module and stored as `(File)-[:DEFINES]->(Function)`, `(File)-[:IMPORTS]->(File)` and `(Test)-[:TARGETS]->(Function)`; each failing test becomes `(Failure)-[:OCCURS_IN]->(Test)`. One Cypher query returns each failing test, the function it targets, its file, and how many verified repairs that file has had. The query and rows are shown on screen and added to the model prompt. After the rerun, `(Repair)-[:MODIFIES]->(File)` and `(Repair)-[:ADDRESSES]->(Failure)` record the outcome, so later runs see the history. If Neo4j is missing or fails, the run continues and says so.
4. One Nosana request returns JSON with `diagnosis`, `explanation` and one edit (`path`, `old`, `new`).
5. The edit is rejected unless it targets `pricing.py` only, is non-empty, and its `old` snippet matches exactly once. Tests are never modified, and no model-supplied command is ever run.
6. The edit is applied inside the sandbox, the original tests are re-uploaded, and the same command reruns.
7. Exit 0 is **VERIFIED**, exit 1 is **NOT VERIFIED**, anything else is **ERROR**. The sandbox is deleted on every path, and a cleanup failure is reported separately with the sandbox ID.

The local fixture file is only read, never written, so every run starts from the original bug.

**Seeded demo defect:** `apply_discount` returns the discount amount instead of the discounted price.

## Tests

```bash
python -m pytest -q
```

These offline tests cover proposal validation, verdict mapping and the full run flow against a local fake sandbox (real pytest, fake providers): a genuine repair, a non-fixing patch, an out-of-scope edit, a provider failure, an already-passing baseline, cleanup, graph extraction, graph context reaching the model, and Neo4j failing at each step without breaking the repair. They test the logic only; they are not evidence of live Daytona or Nosana integration.

## Integration status

- Daytona and Nosana: implemented and verified in a live end-to-end run (VERIFIED).
- Neo4j: implemented as optional graph context, added after the MVP spec (mvp.md lists it as deferred); live run pending verification.

VERIFIED means the designated tests passed after the patch; it does not prove arbitrary code is correct.
