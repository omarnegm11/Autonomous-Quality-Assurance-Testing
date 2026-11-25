"""Fixed integration baseline. This is deliberately NOT an LLM agent run."""
from .schemas import Decision


def run_baseline(tools):
    def do(role, tool, **arguments):
        decision = Decision(agent=role, goal_id="FIXED_BASELINE", tool=tool, arguments=arguments,
                            reason="Deterministic integration baseline; no model selected this action")
        tools.store.append("decisions.jsonl", {"decision": decision.model_dump(), "selection": "fixed_baseline"})
        result = tools.execute(decision)
        if not result.ok:
            raise RuntimeError(f"Baseline tool failed: {result.observation}")
        return result.observation

    if tools.cfg.workflow == "authentication":
        do("ui_explorer", "observe_page")
        do("api_explorer", "describe_endpoint")
        do("auth_db_explorer", "inspect_session_status")
        do("auth_db_explorer", "verify_logout")
        do("diagnosis", "read_run_summary")
        if not tools.board.failures:
            do("diagnosis", "submit_run_review", conclusion="Fixed verification: login protection and logout revocation checks passed.")
        tools.board.stop_reason = "Fixed authentication verification completed"
        return
    do("ui_explorer", "observe_page")
    do("ui_explorer", "select_test_identity", identity="user_a")
    do("ui_explorer", "fill_field", testid="new-title", value=f"{tools.board.run_id} incomplete task")
    do("ui_explorer", "click_element", testid="create")
    record = next(iter(tools.board.records.values()))
    task_id = record["id"]
    do("ui_explorer", "reload_page")
    do("api_explorer", "check_response_contract", task_id=task_id)
    do("auth_db_explorer", "verify_task_record", task_id=task_id)
    do("ui_explorer", "verify_ui_isolation", task_id=task_id)
    do("api_explorer", "call_allowed_endpoint", operation="probe_owner", task_id=task_id)
    do("auth_db_explorer", "verify_ownership", task_id=task_id)
    do("auth_db_explorer", "inspect_session_status")
    do("auth_db_explorer", "verify_logout")

    def diagnose():
        for rule in tools.board.failures.copy():
            if rule in tools.board.diagnoses:
                continue
            do("diagnosis", "read_run_evidence", rule_id=rule)
            do("diagnosis", "request_replay", rule_id=rule)
            query = "may_edit" if rule == "OWNER_ONLY_EDIT" else "visibleTasks"
            hits = do("diagnosis", "search_source", query=query)["hits"]
            file = next((h["file"] for h in hits if h["file"] in ("authorization.py", "tasks.js")), "")
            source = do("diagnosis", "read_source_range", file=file, start=1, end=5)
            do("diagnosis", "run_targeted_check", rule_id=rule)
            # Derive location from tool-returned lines; no fabricated path/line/snippet.
            line = next(row["line"] for row in source["lines"] if "return" in row["text"])
            explanation = ("Ownership predicate permits the second identity's edit; response and saved values reproduce the violation."
                           if rule == "OWNER_ONLY_EDIT" else "The display selector filters saved tasks; targeted completed/incomplete observations support the association.")
            do("diagnosis", "submit_diagnosis", rule_id=rule, explanation=explanation, file=file, line=line)

    diagnose()
    do("api_explorer", "call_allowed_endpoint", operation="edit", task_id=task_id, title=f"{tools.board.run_id} owner edited")
    do("auth_db_explorer", "compare_saved_values", task_id=task_id)
    diagnose()
    do("api_explorer", "call_allowed_endpoint", operation="delete", task_id=task_id)
    do("api_explorer", "check_lifecycle", task_id=task_id)
    tools.board.stop_reason = "Fixed verification suite completed"
