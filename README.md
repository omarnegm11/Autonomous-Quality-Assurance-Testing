# Cooperative QA agent PoC

A CLI implementation of the supplied demo plan: a real authenticated task app, PostgreSQL, Playwright, HTTPX, four role-specific LangChain model/tool loops, LangGraph scheduling, shared discoveries, bounded replay, source inspection and evidence reports.

**Live Groq execution is configured locally.** `baseline` runs exercise the real application and tools using a clearly labelled fixed suite; `llm` runs use model-selected actions through the same tool layer. See [implementation status](docs/implementation-status.md) for actual run evidence and remaining validation. New checkouts must configure their own provider key.

## Local setup (Windows / PowerShell)

Use Python 3.11 or newer (verified here with 3.13). From this repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pip install -e . --no-deps
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.local\browsers'
.\.venv\Scripts\python -m playwright install chromium --no-shell
.\.venv\Scripts\python scripts\local_postgres.py setup
.\.venv\Scripts\python -m qa_poc.cli doctor
```

The browser stays inside `.local/browsers` on the workspace drive. The runner uses full Chromium in headless mode, so the separate headless-shell download is unnecessary. `BROWSERS_PATH` can change that location.

The PostgreSQL helper downloads pinned 17.6 test binaries from Maven Central, verifies the published SHA-256, and starts a workspace-local cluster on `127.0.0.1:55432`. It creates:

| Database / role | Purpose |
| --- | --- |
| `qa_app_test` / `qa_app` | Backend sessions, synthetic tasks and request logs |
| `qa_app_test` / `qa_verifier` | SELECT-only verification; default read-only transactions |
| `qa_agent_state` / `qa_state` | Run snapshots and coordination state |

The helper generates local test credentials in ignored `.env` and preserves an existing `.env`. It never configures a model key. `.local/` contains the cluster, admin credential file, build snapshots and downloaded binaries; keep it local. No system service is installed.

```powershell
.\.venv\Scripts\python scripts\local_postgres.py stop
.\.venv\Scripts\python scripts\local_postgres.py start
```

For an existing PostgreSQL installation, copy `.env.example` to `.env`, create separate application/state databases and login roles, then grant `qa_verifier` CONNECT on the test DB, USAGE on its public schema, and SELECT on the app's tables. Set `ALTER ROLE qa_verifier SET default_transaction_read_only = on`. Grant future table reads with `ALTER DEFAULT PRIVILEGES FOR ROLE qa_app IN SCHEMA public GRANT SELECT ON TABLES TO qa_verifier`. The backend uses `TEST_DATABASE_URL`; the verifier must target that same database through a separate role. The settings reject using one database for both stores.

## Verify real tools without a model

```powershell
.\.venv\Scripts\python -m qa_poc.cli evaluate --mode baseline
```

`evaluate --mode random` uses seeded random valid actions with the same tools and budgets. `--repeats 3` works in all modes; random seeds increment per repetition from `RANDOM_SEED` (default 7). Random runs can leave checks inconclusive when their budget ends. Their reports keep reproduction and cause unconfirmed unless evidence actually supports them. Compare coverage, observed failures and resource usage rather than assuming every mode completes the same checks.

This prepares three separate source builds, starts each backend in turn, runs real browser/API/DB checks, investigates observed failures, cleans up, and writes `runs/evaluation_*/comparison.json`. The evaluator knows the seeded build labels; specialists receive opaque content-based versions and source files, without labels or seeded locations.

The clean build should pass all nine explicit checks. The visibility build should violate task persistence and UI visibility after edit. The ownership build should allow B's edit and show changed stored values. Results always follow actual responses; timeouts and missing checks remain inconclusive. A source location is reported only after matching-version checks, source reads, successful replay and a targeted check.

## Configure and run the LLM team

Edit the following values in `.env`:

```dotenv
LLM_PROVIDER=groq
LLM_MODEL=qwen/qwen3.8-27b
LLM_API_KEY=YOUR_GROQ_KEY
LLM_BASE_URL=https://api.groq.com
LLM_REASONING_EFFORT=none
LANGSMITH_TRACING=false
LANGCHAIN_TRACING_V2=false
```

Create a Groq API key at https://console.groq.com/keys and put it in `LLM_API_KEY`. **No LangChain or LangSmith key is required.** LangChain runs as a local library. Optional hosted LangSmith tracing stays disabled; local structured traces are always recorded.

The project includes the native `langchain-groq` adapter and retains `langchain-openai` for other configurations. `LLM_PROVIDER` selects a LangChain adapter; `LLM_BASE_URL` optionally overrides its endpoint. With `groq`, use the root `https://api.groq.com` or leave it blank for the SDK default. The runner also accepts the earlier compatibility URL ending in `/openai/v1` and removes that suffix because the native SDK appends it. The selected Qwen model supports tool calling, and `LLM_REASONING_EFFORT=none` selects its instruct mode so output tokens go toward actions. Clear this value when choosing a model that does not support the setting. Groq requests disable parallel tool calls because specialist actions execute one at a time.

Groq offers a free plan with request/token quotas; use the free plan if you want to avoid API charges. Quotas and model access depend on your account. Temporary HTTP 429 responses are retried at most `LLM_RATE_LIMIT_RETRIES=3` times, honoring the server delay up to `LLM_MAX_RETRY_WAIT_SECONDS=60` and the run deadline. Provider waits appear in the terminal and `provider_events.jsonl`. This repeats only the model request, never completed application mutations. SDK retries stay disabled. A long quota wait or exhausted retry budget stops the run with incomplete checks and cleanup. Prompts retain current shared state and the most recent complete tool exchange; the complete history stays in local traces. The Groq example allows 600 seconds and 60,000 total model tokens while keeping the 60-unit tool limit. See [Groq rate limits](https://console.groq.com/docs/rate-limits), [Qwen model options](https://console.groq.com/docs/model/qwen/qwen3.8-27b), and [LangChain's Groq integration](https://docs.langchain.com/oss/python/integrations/chat/groq).

```powershell
.\.venv\Scripts\python -m qa_poc.cli evaluate --mode llm
```

For a single interactive deployment:

```powershell
.\.venv\Scripts\python -m qa_poc.cli build clean
.\.venv\Scripts\python -m qa_poc.cli serve
```

Open `http://127.0.0.1:8000`. Sign in as `user_a` or `user_b` with the generated test passwords from `.env`. In another terminal:

```powershell
.\.venv\Scripts\python -m qa_poc.cli run --mode llm
```

To watch the UI explorer in a visible browser:

```powershell
.\.venv\Scripts\python -m qa_poc.cli run --mode llm --headed
```

The browser shows a cursor moving to actual targets, clicks, typing, account changes and reloads. A small status overlay names the active specialist or a provider-quota wait. API and DB specialists use their real network/database tools while their status remains visible in the browser. Isolated accounts keep separate browser contexts. The browser closes after cleanup.

All four specialists participate in `llm` mode: Diagnosis reads the run summary early, then submits a final review after checks finish. A clean review records no invented bug; replay/source diagnosis still requires an observed failure. The overlay and final report show actual per-agent tool counts. If a browser window is accidentally closed, the runner restores isolated sessions up to `BROWSER_MAX_RESTARTS=2` times. Use Ctrl+C in the runner terminal to cancel and finalize cleanup/reporting.

Set `BROWSER_HEADLESS=false` in `.env` to make this the default. `BROWSER_SLOW_MO_MS=120` and `BROWSER_TYPE_DELAY_MS=20` control playback speed of actual interactions. `--headless` hides the browser for automated runs. These settings do not replace model decisions with recorded actions.

The terminal prints an absolute `report.md` path as soon as a run starts. The file initially says RUNNING, then is replaced by the final report after checks and cleanup, including handled errors or Ctrl+C. Completion prints pass/fail/blocked counts. To print a saved report in the terminal:

```powershell
.\.venv\Scripts\python -m qa_poc.cli report
# Or select a specific run:
.\.venv\Scripts\python -m qa_poc.cli report --run-id run_0e0bccd1b646
```

`APP_SOURCE` must point at the build served by that backend. `build visibility --output .local/builds/my-visibility` and `build ownership --output .local/builds/my-ownership` prepare alternative builds; set `APP_SOURCE` before starting the backend and runner. Builds are never overwritten. Stop a manually running backend before `evaluate`, which needs the same configured port.

Skip `build clean` if `.local/builds/clean` is already prepared in this workspace.

## How agents cooperate

The scheduler routes work by unmet goals, available records, completed prerequisites and uninvestigated failures. Enabled specialists alternate one model-selected action per activation by default, while the UI browser stays visible. `SPECIALIST_ACTIONS_PER_ACTIVATION` can increase this to at most six calls. A scheduler node is ordinary Python; it is not described as a fifth autonomous agent. Execution remains sequential so mutations do not race.

| Specialist | Tools / responsibility |
| --- | --- |
| UI explorer | Observe DOM, select isolated account context, fill allowed fields, click observed controls, reload, screenshot, check B's UI |
| API explorer | Describe real OpenAPI endpoints, call allowlisted operations, verify exact response values and deletion |
| Auth/DB explorer | Verify login/revocation, parameterized read-only record checks, consume API ownership probe, compare edit across layers |
| Diagnosis | Review the run summary on every run; investigate actual failures through replay/source tools; submit a final evidence review even when no bug is found |

UI creation captures the actual POST response. Its ID, owner and expected values enter a Pydantic blackboard and a discovery message. Other specialists consume that ID. The backend tags creations with the runner's `X-QA-Run` header. Tools reject arbitrary URLs, SQL, paths and records outside the run's synthetic data.

The agent state database stores the complete blackboard snapshot: observations, messages, checks, records, reservations, failures and diagnoses. Execution is sequential. Mutations reserve a record/session for the action; deletion stays blocked until cross-layer checks and diagnosis complete. Cleanup reconciles committed creations through the API by exact run ID, deletes only those records and releases reservations, including on cancellation and budget exhaustion.

This is a cooperative MAS with blackboard communication and mediated coordination. It borrows goal/belief/intention and message concepts without claiming formal BDI or FIPA-ACL compliance.

## Evidence, rules and limits

Every run produces:

```text
runs/run_*/
  run_manifest.json     # opaque app version, git commit, content digest, model and limits
  decisions.jsonl      # selector and rationale, explicitly model or baseline
  tool_events.jsonl    # proposal, actual result, model tool-call linkage
  shared_state.json    # persisted blackboard (also saved in PostgreSQL)
  assertions.json
  report.json
  report.md
  cleanup.json
  screenshots/
  api_evidence/
  db_evidence/
  source_evidence/
```

Model runs additionally record `prompts.json`, `model_inputs.jsonl` and public model messages with tool-call IDs. Raw session cookies and passwords stay inside the tool layer. Screenshots mask password inputs. Reports separate observed/confirmed behavior from likely/unconfirmed cause, and include reproduction steps, real source lines, targeted checks and uncertainty.

Rules cover login (200), revoked old-session access (401), UI persistence, exact API/DB saved values, owner edit across layers, deletion (204 then 404 and absent row), B's ownership probe (403 and unchanged row) and UI isolation. Python defines assertion meanings; the model selects actions and instances. Tool errors do not create confirmed defects.

Code defaults are 300 seconds, 60 execution units including nested HTTP/replay requests, one decision per activation (configurable up to six), one retry for transient GET failures, two replays per failure, eight source files, and 30,000 model tokens. The Groq `.env.example` allows 600 seconds and 60,000 model tokens for provider waits. Model input usage is estimated before calls and reconciled with provider usage afterward; provider tokenization can differ. Cleanup has a separate bounded allowance and still runs after the exploration limit. Local traces record usage, not dollar costs.

Source verification uses both the repository commit and a SHA-256 digest of the exact copied frontend, authorization and backend sources. This supports uncommitted implementation builds without pretending the commit alone identifies them. A drift or mismatch blocks source conclusions.

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q
$env:QA_INTEGRATION = '1'
.\.venv\Scripts\python -m pytest -q
```

Integration tests require the local cluster and installed Chromium. They exercise clean/seeded builds, actual cross-specialist record sharing, source grounding, exact assertion outcomes, secret-free artifacts, read-only verifier enforcement and removal of run-owned rows. They do not call a model. Integration artifacts use a workspace-local test directory.

CLI exit 2 means setup, configuration or incomplete verification requires attention. An observed application defect is represented in the report rather than mistaken for a runner error.

The implementation follows the framework APIs documented in [LangChain models](https://docs.langchain.com/oss/python/langchain/models) and [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api). The local PostgreSQL helper uses the [Zonky test-binary distribution](https://github.com/zonkyio/embedded-postgres-binaries).

## Web dashboard

```powershell
.\.venv\Scripts\python -m qa_poc.cli dashboard
```

Open **http://127.0.0.1:8080** for saved project configurations, secure credential settings, Start/Stop controls, all four agents? real activity, an expandable live browser, and reports with evidence and source findings. Keep the test application on port 8000 and PostgreSQL running separately.

See [Dashboard setup and supported workflows](docs/dashboard.md).
