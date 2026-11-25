import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
import httpx
from sqlalchemy import create_engine, text
from .baseline import run_baseline
from .builds import build
from .config import Settings
from .reporting import report
from .schemas import Blackboard
from .storage import Store
from .tools import BudgetExpired, ToolLayer


def run_one(cfg, mode):
    if mode == "llm" and (not cfg.llm_api_key.get_secret_value() or cfg.llm_model in ("", "YOUR_MODEL_ID")):
        raise ValueError("Actual LLM agent execution is pending. Configure LLM_PROVIDER, LLM_MODEL and LLM_API_KEY; use --mode baseline to verify real tools without a model.")
    board = Blackboard(run_id=cfg.dashboard_run_id or "run_" + uuid4().hex[:12], mode=mode)
    if cfg.workflow == "authentication":
        board.target_rules = ["LOGIN_PROTECTED", "LOGOUT_REVOKES_SESSION"]
    store = Store(cfg, board)
    print(f"Run {board.run_id}; mode={mode}; evidence={store.folder}", flush=True)
    report_path = (store.folder / "report.md").resolve()
    report_path.write_text(f"# QA run {board.run_id}\n\nStatus: RUNNING. This is a placeholder; final checks and cleanup are written when the run ends.\n", encoding="utf-8")
    print(f"Report (finalized when run ends): {report_path}", flush=True)
    print(f"Browser: {'hidden' if cfg.browser_headless else 'visible with live cursor and typing'}", flush=True)
    tools = ToolLayer(cfg, board, store)
    interrupted = False
    try:
        tools.setup()
        if mode == "baseline":
            run_baseline(tools)
        elif mode == "random":
            from .random_baseline import run_random
            run_random(tools, cfg.random_seed)
        else:
            from .agents import Team
            Team(cfg, tools).run()
    except KeyboardInterrupt:
        interrupted = True
        board.stop_reason = "Interrupted by user; cleanup performed"
    except BudgetExpired as exc:
        board.stop_reason = str(exc)
    except Exception as exc:
        board.stop_reason = f"Setup/run blocked: {type(exc).__name__}"
        store.append("errors.jsonl", {"error_type": type(exc).__name__, "message": str(exc)[:1500]})
    finally:
        try:
            cleanup = tools.cleanup()
        except Exception as exc:
            board.reservations.clear()
            cleanup = {"outcome": "INCONCLUSIVE", "records": [], "reservations_released": True,
                       "error_type": type(exc).__name__}
            store.write("cleanup.json", cleanup)
        result = report(board, store, cleanup)
        store.engine.dispose()
    counts = {name: sum(c["outcome"] == name for c in result["assertions"]) for name in ("PASS", "FAIL", "BLOCKED", "INCONCLUSIVE")}
    print("Checks: " + ", ".join(f"{name}={count}" for name, count in counts.items()), flush=True)
    print("Agents: " + ", ".join(f"{role}={activity['executed_actions']} tools"
        for role, activity in result["agent_activity"].items()), flush=True)
    print(f"Stop: {board.stop_reason}; cleanup={cleanup['outcome']}", flush=True)
    print(f"Report: {report_path}", flush=True)
    if interrupted:
        raise KeyboardInterrupt
    return result, store.folder


def evaluate(cfg, mode, repeats):
    evaluation_id = "evaluation_" + uuid4().hex[:10]
    folder = cfg.runs_dir / evaluation_id
    folder.mkdir(parents=True)
    results = []
    for repetition in range(repeats):
        for kind in ("clean", "visibility", "ownership"):
            source = build(kind, Path(".local/builds") / evaluation_id / f"{repetition}_{kind}").resolve()
            env = os.environ.copy()
            env["APP_SOURCE"] = str(source)
            # Serialize deployments: same accounts, rules, DB and limits for each build.
            app_url = httpx.URL(cfg.app_url)
            log_path = folder / f"server_{repetition}_{kind}.log"
            with log_path.open("w", encoding="utf-8") as output:
                process = subprocess.Popen([sys.executable, "-m", "uvicorn", "qa_poc.server:create_app", "--factory",
                    "--host", str(app_url.host), "--port", str(app_url.port or 80)], env=env, stdout=output, stderr=output,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                try:
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError(f"Backend failed to start: {log_path}")
                        try:
                            version = httpx.get(cfg.app_url + "/version", timeout=1)
                            if version.status_code == 200:
                                expected = json.loads((source / "manifest.json").read_text())
                                if version.json() != expected:
                                    raise RuntimeError("Port already serves a different application; stop it or change APP_URL")
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.2)
                    else:
                        raise RuntimeError("Backend startup timed out")
                    print(f"\nEvaluator case={kind} repetition={repetition + 1}; label is not passed to agents", flush=True)
                    result, report_folder = run_one(cfg.model_copy(update={"app_source": source, "random_seed": cfg.random_seed + repetition}), mode)
                    results.append({"case": kind, "repetition": repetition + 1, "run_id": result["run_id"],
                        "execution_mode": mode, "assertions": {c["rule_id"]: c["outcome"] for c in result["assertions"]},
                        "failures": [b["rule_id"] for b in result["bug_candidates"]], "cleanup": result["cleanup"]["outcome"],
                        "report": str(report_folder / "report.md"), "budgets_used": result["budgets_used"]})
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    summary = {"evaluation_id": evaluation_id, "mode": mode, "actual_llm_execution": "ATTEMPTED" if mode == "llm" else "PENDING_CREDENTIALS",
               "results": results, "limits": {"actions": cfg.max_actions, "seconds": cfg.max_seconds, "model_tokens": cfg.max_model_tokens}}
    (folder / "comparison.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Comparison: {folder / 'comparison.json'}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Cooperative QA PoC; real tools with explicit baseline/LLM modes")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("build")
    prepare.add_argument("kind", choices=("clean", "visibility", "ownership"))
    prepare.add_argument("--output", type=Path)
    commands.add_parser("doctor")
    commands.add_parser("serve")
    dashboard = commands.add_parser("dashboard", help="Open the local QA control dashboard")
    dashboard.add_argument("--port", type=int, default=8080)
    view_report = commands.add_parser("report", help="Print the most recent saved report, or select one run")
    view_report.add_argument("--run-id")
    for name in ("run", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("--mode", choices=("llm", "baseline", "random"), default="llm")
        browser_options = command.add_mutually_exclusive_group()
        browser_options.add_argument("--headed", dest="browser_headless", action="store_false", help="Watch browser actions live")
        browser_options.add_argument("--headless", dest="browser_headless", action="store_true", help="Hide browser windows")
        command.set_defaults(browser_headless=None)
        if name == "evaluate":
            command.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    try:
        if args.command == "build":
            print(build(args.kind, args.output or Path(".local/builds") / args.kind))
            return
        if args.command == "dashboard":
            import uvicorn
            uvicorn.run("qa_poc.dashboard:create_app", factory=True, host="127.0.0.1", port=args.port)
            return
        cfg = Settings()
        if getattr(args, "browser_headless", None) is not None:
            cfg = cfg.model_copy(update={"browser_headless": args.browser_headless})
        if args.command == "report":
            if args.run_id:
                folder = (cfg.runs_dir / args.run_id).resolve()
                if folder.parent != cfg.runs_dir.resolve():
                    raise ValueError("Run ID must name a direct run folder")
                path = folder / "report.md"
            else:
                candidates = list(cfg.runs_dir.glob("run_*/report.md"))
                if not candidates:
                    raise ValueError("No saved reports yet")
                path = max(candidates, key=lambda p: p.stat().st_mtime)
            if not path.is_file():
                raise ValueError("Report not found for the selected run")
            print(f"Report: {path.resolve()}\n", flush=True)
            print(path.read_text(encoding="utf-8"))
        elif args.command == "doctor":
            for label, url in (("application", cfg.test_database_url), ("agent state", cfg.agent_state_database_url), ("read-only verifier", cfg.verifier_database_url)):
                engine = create_engine(url.get_secret_value())
                with engine.connect() as conn:
                    print(f"{label}: PostgreSQL {conn.execute(text('SHOW server_version')).scalar_one()}, database={conn.execute(text('SELECT current_database()')).scalar_one()}")
                engine.dispose()
            print("LLM execution: CONFIGURED, not yet validated" if cfg.llm_api_key.get_secret_value() else "LLM execution: PENDING_CREDENTIALS")
        elif args.command == "serve":
            import uvicorn
            url = httpx.URL(cfg.app_url)
            uvicorn.run("qa_poc.server:create_app", factory=True, host=str(url.host), port=url.port or 8000)
        elif args.command == "run":
            result, _ = run_one(cfg, args.mode)
            if any(c["outcome"] in ("BLOCKED", "INCONCLUSIVE") for c in result["assertions"]):
                raise SystemExit(2)
        else:
            if not 1 <= args.repeats <= 10:
                raise ValueError("Use 1..10 repeats")
            if args.mode == "llm" and not cfg.llm_api_key.get_secret_value():
                raise ValueError("Actual LLM agent execution pending credentials")
            summary = evaluate(cfg, args.mode, args.repeats)
            if any(r["cleanup"] != "PASS" or any(o in ("BLOCKED", "INCONCLUSIVE") for o in r["assertions"].values()) for r in summary["results"]):
                raise SystemExit(2)
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
