# PatchPilot — Hackathon MVP Spec Sheet

**Version:** 1.1 · **Date:** 31 October 2026 · **Build budget:** one hour

**Status:** Scoped MVP; product implementation and live integration verification pending.

**Public repository:** [Viamance/patchpilot67](https://github.com/Viamance/patchpilot67) (spec originally drafted in [HappyCool121/patchpilot](https://github.com/HappyCool121/patchpilot))

> From a failing test to an AI-proposed, sandbox-verified patch—with the evidence visible.

## 1. Problem and solution

Developers spend time interpreting test failures, editing code, and rerunning tests. An AI suggestion alone does not establish that a fix works.

PatchPilot demonstrates one complete repair cycle: reproduce a known failure in Daytona, ask a model served through Nosana for a small fix, apply it inside the sandbox, and rerun the same tests. The developer sees the diagnosis, exact diff, and before/after results before deciding whether to use the change.

**Target user:** a developer debugging a failing Python unit test.

**Demo promise:** one prepared bug, one repair attempt, and a result supported by actual test execution.

## 2. Required MVP scope

| Capability | Minimum deliverable |
| --- | --- |
| Start a repair | One Streamlit screen with a **Repair demo bug** button and visible progress. |
| Reproduce the failure | Upload a bundled Python fixture to a fresh Daytona sandbox and run `python -m pytest -q`; capture output and exit code. |
| Generate a repair | Make one real Nosana inference request using the failing output, implementation, and read-only tests as context. |
| Apply a constrained edit | Validate the response and change only the fixture's implementation file inside the sandbox. |
| Verify the result | Rerun the identical command against unchanged tests. |
| Show the evidence | Display the diagnosis, unified diff, before/after output, and final verdict. |

**Prepared fixture:** a discount-calculation function with an incorrect formula. Tests cover a normal discount, zero discount, and full discount. For example, a price of 100 with a 20% discount must produce 80. Clearly identify this as a seeded demo defect.

There is one fixture and one attempt per run. A new manual run starts from the original fixture in a fresh sandbox.

## 3. Demo journey and sponsor roles

1. Open PatchPilot and view the prepared fixture.
2. Click **Repair demo bug**.
3. Watch Daytona reproduce the failing tests.
4. Review the Nosana-generated diagnosis and proposed edit.
5. Watch Daytona rerun the same tests after applying the edit.
6. Inspect the diff and verification evidence. The original local fixture remains unchanged.

| Integration | Role in this submission |
| --- | --- |
| **Daytona** | Isolated workspace, fixture execution, patch application, test rerun, and sandbox cleanup. |
| **Nosana** | Hosted inference producing a concise diagnosis and structured code edit. |

Neo4j is future work and is not required or claimed as an MVP integration.

## 4. Minimal implementation contract

**Stack:** Python, Streamlit, the stable Daytona Python SDK, an HTTP client for Nosana, and pytest inside the sandbox. Streamlit calls the coordinator directly; no separate API service or database is required.

**Data flow:** Streamlit → Daytona baseline → Nosana proposal → Daytona verification → result screen.

- Store only the latest run in session memory. Show progress through reproducing, proposing, applying, and verifying.
- Keep `DAYTONA_API_KEY` and `NOSANA_API_KEY` in server-side environment configuration. Never commit or display credentials.
- Check provider access at setup. Use a currently available Nosana model from its model list and retain its ID with the run evidence. Nosana requires an API key with `inference:use` permission and usable credits.
- Request JSON containing `diagnosis`, `explanation`, and one `edits` entry with `path`, `old`, and `new`. Reject malformed output, a non-allowlisted path, an empty edit, or an old snippet that does not match exactly once.
- Never modify tests or execute model-supplied commands. Run generated code only inside Daytona using the fixed test command.
- Set finite provider and command timeouts. Make one repair request per run, with no automatic retry loop.
- Attempt sandbox deletion on every exit path. Report cleanup failure separately and retain the sandbox ID for cleanup; preserve captured evidence after deletion.

## 5. Success criteria

| Verdict | Required condition |
| --- | --- |
| **VERIFIED** | Baseline pytest exits with code 1, a non-empty allowed patch is applied, and the same tests rerun with exit code 0. |
| **NOT VERIFIED** | The patch is applied, but the rerun still reports test failures. |
| **NO FAILURE REPRODUCED** | Baseline exits with code 0; stop without requesting a repair. |
| **ERROR** | Provider, setup, timeout, malformed patch, or test-execution error prevents a valid result. Other nonzero pytest exit codes are execution errors, not proof of a reproduced bug. |

Verification means the designated tests passed; it does not establish that arbitrary code is correct.

**Acceptance checks:** one genuine end-to-end repair; a non-fixing patch; invalid/out-of-scope edits; provider failure; an already-passing baseline; and cleanup on success and failure. Simulated failures may test error handling but must not be presented as live sponsor evidence.

## 6. One-hour delivery budget and submission package

| Budget | Outcome |
| --- | --- |
| 0–5 minutes | Confirm credentials, model availability, and sandbox access. |
| 5–15 minutes | Bundle the fixture and reproduce the failure in Daytona. |
| 15–30 minutes | Connect Nosana, validate the edit, and rerun tests. |
| 30–40 minutes | Complete the single-screen UI and evidence display. |
| 40–50 minutes | Freeze features, check failure cases, and record a short walkthrough. |
| 50–60 minutes | Finish setup instructions and submission copy, push the product, and verify public repository access. |

**Required at submission:** runnable product source in the public repository; dependency and environment setup instructions; a command to launch the app; the bundled fixture; this spec; and a 90–120-second recorded walkthrough of a real run with credentials excluded. Clearly label recorded evidence.

The public repository currently contains the specification only. A public repository URL is not evidence that the product has been implemented. Document the actual implementation and integration status when preparing the submission.

**Landing page:** deferred. Its future public URL is separate from the runnable product source; hosted product deployment is not required for this MVP.

**Pitch:** “PatchPilot turns a failing Python test into an AI-proposed fix and checks that fix in an isolated sandbox. Daytona runs the code, Nosana proposes the repair, and the developer gets the diff and test evidence to review.”

## 7. Explicitly deferred

Neo4j and graph retrieval, arbitrary/private repositories, multiple languages, repository upload forms, GitHub OAuth, CI webhooks, automatic PRs or merges, multiple repair attempts, persistent history, billing, and production hosting.

If either provider cannot be used, report the integration as incomplete. Do not substitute fabricated logs, a hardcoded repair presented as AI output, or a false successful verdict.

## Implementation references

- [Daytona stable Python SDK](https://www.daytona.io/docs/en/python-sdk/)
- [Daytona process execution](https://www.daytona.io/docs/en/process-code-execution/)
- [Nosana hosted inference](https://learn.nosana.com/llm/)
- [Nosana API permissions](https://learn.nosana.com/api/scopes.html)
