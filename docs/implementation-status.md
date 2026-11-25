# Implementation and verification status

The implementation is present and the local Groq key is configured. Real model-selected UI/API/DB execution is now observed; the live run evidence below distinguishes it from fixed-baseline validation. New checkouts must configure their own provider key.

| Milestone | Implementation | Validation |
| --- | --- | --- |
| M1 environment | Real task frontend/backend, isolated PostgreSQL databases, two identities, request logs, matching source snapshots | Local database connectivity verified |
| M2 tools | Browser/HTTP/SELECT-only verifier/source tools, role/argument/path/record restrictions | Contract and real PostgreSQL/Chromium integration checks passed |
| M3 UI agent | Role-specific model/tool loop, observations and history, bounded activation | Live Groq decisions created a task and selected reload/isolation checks |
| M4 team | Four specialist loops, LangGraph scheduler/subgraphs, blackboard messages, reservations and dependency holds | Completed visible Groq run: all four specialists executed and all nine checks passed |
| M5 diagnosis | Fresh-context replay, actual source search/ranges, version verification, targeted check, reports | Real-tool baseline reproduced the visibility and ownership defects and read matching source |
| M6 evaluation | Three-build evaluator and fixed suite, repeat support, saved artifacts | Full three-build LLM comparison and live-demo rehearsal remain outstanding |

## Remaining validation and limitations

- Run the same three-build evaluation in `llm` mode and inspect decision variability. The local key is now configured; provider execution no longer depends on missing credentials.
- Compare repeated LLM runs against both the fixed suite and included seeded random valid-action comparator under identical settings.
- Native Groq and OpenAI adapters are included. Other providers may require an extra package and parameter adjustment.
- Replay supports the demonstrated UI persistence/edit and ownership failures. Other rules retain observed status and an unconfirmed cause until a suitable replay template exists.
- The local dashboard is implemented (see below). Automatic source fixes, broad website compatibility, production integration, parallel exploration and external message delivery remain outside the current implementation.
- Cleanup is guaranteed through Python `finally` for handled failure/cancellation/budgets. A forced process kill or machine shutdown cannot execute cleanup; use the recorded run ID to recover its synthetic data.

## Verified on 25 November 2026

Python 3.13.5, PostgreSQL 17.6, Playwright Chromium 159.0.8154.6, pinned dependencies in `requirements.txt`.

`QA_INTEGRATION=1 python -m pytest -q`: **11 passed**. This includes database separation, strict proposal validation, distinct build digests, source traversal/drift rejection, secret redaction, budget/deletion holds, a labelled observation-dependent model test double with preserved tool-call IDs, the three-build real-tool comparison, budget cleanup preserving an unrelated task, and interruption cleanup releasing reservations.

The model test double verifies graph mechanics; it is not evidence of real LLM autonomy.

Saved fixed-suite comparison: `runs/evaluation_32f0996c4e/comparison.json`.

| Case | Actual results | Reproduction / source | Execution units | Cleanup |
| --- | --- | --- | --- | --- |
| Clean | 9 PASS; no bugs | No defect invented | 27 / 60 | PASS |
| Visibility | Persistence and edited-task visibility FAIL; other checks PASS | Both reproduced; real `tasks.js` line 2 read; completed/incomplete targeted check | 54 / 60 | PASS |
| Ownership | Ownership FAIL; other checks PASS | Two successful reproductions; real `authorization.py` line 2 read | 41 / 60 | PASS |

These locations describe these generated builds; the tools derive locations from source results. The implementation does not hardcode the lines into reports. Each report records the exact build digest and distinguishes confirmed behavior from a likely cause.

Local PostgreSQL connection checks passed for the backend, agent state and read-only verifier. A verifier DELETE attempt was rejected. After the initial integration tests, the test application contained zero task rows. At that initial stage, no model key was configured, and the CLI correctly stopped before making a model call. Live Groq results are recorded below.

The seeded random-action comparison is saved at `runs/evaluation_50eac57799/comparison.json`. All cases stopped at the same 60-unit limit and cleaned up successfully. The clean case had seven passes and two unexercised/inconclusive lifecycle checks, with no fabricated defect. The visibility case observed persistence failure. The ownership case observed the forbidden edit and subsequent saved-value mismatches. Random reports retained observed/unconfirmed status; they did not claim replay or source diagnosis. The incomplete checks correctly caused the evaluator to return a nonzero status.

Source/runtime bytecode compilation and Git whitespace checks also passed. Saved local run artifacts are the authoritative execution evidence; they are ignored by Git, along with credentials, browser binaries and database files.

## Groq configuration update

The local configuration and example now select the native Groq adapter (`LLM_PROVIDER=groq`) with `qwen/qwen3.8-27b`. `langchain-groq` and its SDK are pinned. Only a Groq key in `LLM_API_KEY` is needed; LangChain runs locally and requires no key. Both external tracing flags remain false, so a LangSmith key is unnecessary.

The selected Qwen model uses `LLM_REASONING_EFFORT=none` for instruct mode and hidden reasoning format. Groq tool requests disable parallel calls to match the sequential executor. Clear the reasoning-effort setting when switching to a model that does not support it. The provider and model remain configurable.

Before the live run, the adapter was tested with a mocked HTTP response verifying the Groq endpoint, role-restricted tools, single-call option, returned tool-call ID, usage accounting, missing-key rejection and tracing disabled without any LangChain/LangSmith key. That test alone did not establish live provider access or autonomous coverage. Free-tier rate limits can leave checks incomplete; cleanup still runs.

After this update, `python -m pytest -q` passed **12 tests**; the three opt-in PostgreSQL/browser integration tests were skipped in this provider-focused run. The adapter test covers native-root, legacy compatibility and SDK-default URLs to prevent duplicated endpoint paths. Dependency checks, compilation and Git whitespace checks passed. The previous real application integration results above still describe the earlier completed runs.

## Live browser and report visibility

Visible execution is available with `run --mode llm --headed`, and the local `.env` now sets `BROWSER_HEADLESS=false`. Real Playwright mouse moves/clicks and sequential typing drive the browser. An on-page cursor and status overlay show the active specialist and provider-quota waits. Accounts remain isolated. Automated runs can use `--headless`. These are presentations of actual tool actions, not prerecorded decisions.

The CLI creates a clearly labelled RUNNING report placeholder and prints its absolute path immediately, then finalizes it after checks and cleanup. Completion prints assertion counts, stop reason, cleanup outcome and the final report path. `python -m qa_poc.cli report` prints the most recently saved report; `--run-id` selects a historical report.

The live Groq run `run_0e0bccd1b646` demonstrated real UI/API/Auth-DB decisions and cross-layer checks, but did not complete: a repeat check after an authorized owner edit used the original title, and subsequent diagnosis exceeded Groq's prompt-size limit. Its historical report is annotated with a runner review: that stale-title candidate is invalid as an application defect. Raw assertions were retained for audit. This run is partial evidence, not a successful full-team comparison.

The runner now advances current expected values from successful owner-requested changes, including browser edits. It does not derive expected values from possibly defective response values. Repeated API/DB/reload checks therefore compare with the correct current expectation. Source-evidence history and OpenAPI observations are compact, with the full data retained in evidence files, to avoid copying full nested run histories into prompts. Temporary 429s receive bounded server-directed waits within the run deadline; completed application tools are not repeated.

Validation after those changes: **16 unit/adapter tests passed** and **4 real PostgreSQL/Chromium integration tests passed**. Integration covered clean/seeded builds, cleanup after budget expiry/interruption, a genuinely visible browser with cursor movement, and repeated API/DB/UI checks after owner editing. Integration servers use separate available ports and preserve unrelated records. At that stage, a complete live-model rerun had not yet been performed. The later result below records its completion; the full defective-build LLM comparison remains outstanding.

## All specialists during visible execution

The normal model run now alternates enabled specialists one action at a time (`SPECIALIST_ACTIONS_PER_ACTIVATION=1`). Diagnosis performs a real `read_run_summary` action early and a `submit_run_review` action after target checks finish, including on clean builds. Replay and source-cause claims still require actual failures. No clean-run defect is fabricated to activate Diagnosis.

The browser overlay and the Markdown/JSON report show each specialist's actual tool counts. The terminal logs scheduling transitions and prints all four counts on completion. A closed browser is restored up to `BROWSER_MAX_RESTARTS=2` times using separate authorized test sessions; presentation errors no longer abort independent API/DB actions. Cancellation remains Ctrl+C in the runner terminal.

Validation: **19 unit/adapter/scheduling tests passed** and **4 real application integration tests passed**. Integration closed and recovered a visible browser, preserved the discovered task ID and isolated sessions, executed tools from all four roles, passed all nine checks, and submitted a clean Diagnosis review with no invented failure. The scheduling tests cover alternating roles and successful UI steps remaining enabled before record creation.

## Completed visible Groq run

`runs/run_b1c6fccaac69/report.md` records a completed **live LLM** run against the clean build: **9 PASS, 0 FAIL, 0 BLOCKED, 0 INCONCLUSIVE**, final Diagnosis review completed, cleanup PASS and no invented application defect.

| Specialist | Actual model-selected tool calls |
| --- | --- |
| UI explorer | 5 |
| API explorer | 6 |
| Auth/DB explorer | 5 |
| Diagnosis | 2 |

The UI browser remained available during the alternating work. Two browser closures were recovered without losing the discovered record or stopping independent checks. Diagnosis selected the early run-summary tool and the final evidence-review tool; this clean run did not require failure replay or a root-cause claim. Live-model investigation of seeded defects and repeated full-team comparisons remain separate validation tasks.

A final regression also verifies that closing an inactive account window does not invalidate a healthy active viewer. The latest unit/adapter/scheduling suite passes 20 tests; the four real integration tests passed during this update. Compilation and whitespace checks passed.

## QA Studio web dashboard

The local dashboard runs at `http://127.0.0.1:8080` with `python -m qa_poc.cli dashboard`. It integrates the existing runner through a separate local control service; the task deployment on port 8000 is unchanged. See [setup and supported scope](dashboard.md).

Implemented: encrypted saved project configurations; target/source/version and account-role inputs; API specification and authentication; separate PostgreSQL connections; model provider settings; requirements; two supported workflows; execution limits; Start/Stop; real four-specialist state and tool events over SSE; expandable Chromium screencast with the actual cursor/actions; saved-run history; structured reports, evidence links, replay steps and read source snippets. No generated demonstration results are injected into the dashboard.

The current workflow adapter retains the task application's source manifest, test identity, selector, endpoint and database contract. Saving configuration does not add compatibility with arbitrary applications. Authentication-only runs report their two selected checks; full task runs report nine. Additional user requirements are model context, not automatically asserted coverage.

Observed end-to-end dashboard results:

- `run_ee52b2946d21`: real fixed suite through dashboard Start; **9 PASS**, cleanup PASS; 21 distinct Chromium frame timestamps observed during browser verification. Report viewing, evidence access and browser expansion verified. This was a fixed suite, not autonomous model execution.
- `run_beeefba3c5ad`: dashboard Stop after task creation; next action cancelled, cleanup PASS, final report written.
- `run_80be44e1e7d1`: actual Groq authentication workflow; **2 PASS**, cleanup PASS, final diagnosis review completed. Model-selected executed tools: UI 1, API 1, Auth/DB 2, Diagnosis 2. The full nine-check live Groq validation recorded earlier remains separate evidence.

Browser checks verified saved-configuration handling, service preflight, report display, desktop and 390px layouts without horizontal overflow, and no JavaScript page errors. Security tests cover DPAPI credential preservation across reloads, no secret echo in public configuration or validation failures, local session/origin/content-type guards, evidence path confinement, source-version mismatch, stop-before-action, and scoped scheduling. Real backend integration checks also passed after the dashboard changes.

Final dashboard verification: **27 unit tests passed** (six opt-in integration tests skipped in that invocation). The original **four integration tests passed**, and the **two new dashboard integration tests passed** separately, including real worker start, live JPEG frames, all-four-role scoped verification, concurrent-start rejection, and Stop cleanup. JavaScript syntax, Python compilation, and Git whitespace checks passed.
