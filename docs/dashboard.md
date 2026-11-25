# QA Studio dashboard

From the repository folder:

```powershell
.\.venv\Scripts\python -m qa_poc.cli dashboard
```

Open **http://127.0.0.1:8080**. Use `--port 8081` to select another port. Keep the test application and PostgreSQL running separately; the dashboard does not replace or restart the target application.

## Configure and run

1. Choose **Project settings** and enter the website URL, matching prepared source folder, optional repository reference, and expected deployment version or commit.
2. Configure the two test identities and roles, API base URL and OpenAPI specification, optional API authentication header, separate application and shared-state databases, and read-only verifier connection.
3. Enter requirements and choose **Task lifecycle & access control** or **Authentication & sessions**. Choose **Autonomous agents** for real model-selected actions. The fixed and random modes are explicitly labelled baselines.
4. Choose the installed model provider (Groq or OpenAI), model ID, optional endpoint/reasoning setting, and credentials. Blank credential inputs retain saved values or inherit the local `.env` configuration. No LangChain key is required.
5. Set action, time, and token limits. Keep the desktop browser hidden to watch it within the dashboard, or turn that option off to also open desktop windows.
6. **Save project**, then **Verify connections**. Source digest/deployment version, API contract, logical database separation, and read-only verifier settings are checked before execution.
7. Select **Run workspace**, then **Start run**. Only one dashboard-controlled run executes at a time.

The current runner supports the existing task application contract: the prepared source manifest, stable UI selectors, task API, database tables, and `user_a`/`user_b` identities. Account roles provide project context; they do not provision or change server permissions. Repository URLs are saved references; source evidence is read from the verified local prepared folder. Additional requirements are sent to all specialists as context; the selected workflow's actual checks determine the reported coverage. Arbitrary websites require an additional application adapter. Random exploration is supported for the full task workflow only.

## Live activity and reports

Agent cards and activity use the actual runner's saved state and tool/provider events, delivered through a session-protected server-sent event feed. The expandable browser uses Chromium `Page.startScreencast` from the active isolated test page. It shows real DOM changes, mouse movements, typing, and scrolling; it is not an iframe of a separate browser session. Credentials remain masked by password inputs. Frames pause while the browser is idle and the final captured frame remains available after a run.

**Stop** writes a cooperative cancellation signal. The current bounded operation may take up to its timeout to return; the next model/tool operation checks cancellation. The worker then reconciles and deletes only its own synthetic records, revokes its sessions, and writes the final report. Do not close the server process to stop a run. A live worker heartbeat also allows a restarted dashboard to rediscover an active worker.

**Show Report** opens actual assertions, observations, cleanup, specialist counts, evidence files, supported replay steps, and read source snippets. Causes remain unconfirmed when replay/source evidence is missing. Markdown can be downloaded. **Test runs** includes existing CLI reports as well as dashboard runs; older CLI runs have no browser recording.

## Credential storage and local access

The dashboard binds to loopback. API requests require an HttpOnly, SameSite session cookie. Cross-origin requests and unexpected hosts are rejected; mutations require JSON. Content Security Policy disallows external scripts and framing. No credentials are returned to configuration forms, passed in process arguments, or included in public logs.

Saved project configurations and credential overrides are encrypted together in ignored `.local/dashboard/projects.bin` using Windows DPAPI, bound to the current Windows account. The configured `.env` remains the fallback and is not rewritten. The backend sends effective settings to an isolated runner over stdin. Readable runtime files contain only run metadata and already-redacted evidence. Run artifacts can still contain application data and source excerpts; use dedicated test environments. This is a local, single-user control panel, not a remotely hosted multi-user service.

## Verification

```powershell
.\.venv\Scripts\python -m pytest -q
$env:QA_INTEGRATION = '1'
.\.venv\Scripts\python -m pytest -q tests/test_integration.py
```

Dashboard unit checks cover encrypted secret preservation, session/origin guards, validation error redaction, evidence traversal protection, version mismatch rejection, cancellation, and authentication scheduling. Real end-to-end dashboard verification uses the actual PostgreSQL test application, Chromium frames, baseline assertions, cleanup, and configured Groq provider; these runs are kept distinct from unit-test doubles.
