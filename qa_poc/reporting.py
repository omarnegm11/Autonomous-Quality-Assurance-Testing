import json
from .schemas import Check, Outcome, RULES


def report(board, store, cleanup):
    for rule in board.target_rules:
        workflow, statement = RULES[rule]
        if rule not in board.assertions:
            board.assertions[rule] = Check(rule_id=rule, workflow_id=workflow,
                outcome=Outcome.BLOCKED if "setup" in board.stop_reason.lower() else Outcome.INCONCLUSIVE,
                expected=statement, actual={"reason": board.stop_reason or "Not exercised"})
    bugs = []
    for rule in board.failures:
        diagnosis = board.diagnoses.get(rule, {})
        related_refs = list(board.assertions[rule].evidence_refs)
        for event in board.action_history:
            args = event["proposal"]["arguments"]
            if (args.get("rule_id") == rule or args.get("task_id") in board.cleanup_records
                    or event["result"]["action_id"] == board.assertions[rule].action_id):
                related_refs.extend(event["result"]["evidence_refs"])
        bugs.append({"run_id": board.run_id, "workflow_id": RULES[rule][0], "rule_id": rule,
            "app_version": board.app_version, "source_commit": board.source_commit,
            "test_role": "user_b" if rule == "OWNER_ONLY_EDIT" else "user_a",
            "assertion": board.assertions[rule].model_dump(mode="json"),
            "behavioral_status": diagnosis.get("behavioral_status", "OBSERVED"),
            "root_cause_status": diagnosis.get("root_cause_status", "UNCONFIRMED"),
            "diagnosis": diagnosis, "evidence_refs": list(dict.fromkeys(related_refs)), "cleanup": cleanup})
    data = {"run_id": board.run_id, "execution_mode": board.mode, "target_rules": board.target_rules,
        "llm_execution": "PENDING_CREDENTIALS" if board.mode != "llm" else "ATTEMPTED_SEE_TRACE",
        "stop_reason": board.stop_reason, "app_version": board.app_version,
        "source_commit": board.source_commit, "source_verified": board.source_verified,
        "assertions": [c.model_dump(mode="json") for c in board.assertions.values()],
        "bug_candidates": bugs, "budgets_used": board.budgets, "cleanup": cleanup,
        "agent_reviews": board.agent_reviews,
        "agent_activity": {role: {**board.agent_status.get(role, {}),
            "executed_actions": sum(e["proposal"]["agent"] == role for e in board.action_history),
            "successful_actions": sum(e["proposal"]["agent"] == role and e["result"]["ok"] for e in board.action_history)}
            for role in ("ui_explorer", "api_explorer", "auth_db_explorer", "diagnosis")},
        "limitations": ["Deterministic baseline cannot establish autonomous model behavior"] if board.mode != "llm" else ["Bounded exploration does not prove universal correctness"]}
    store.write("assertions.json", data["assertions"])
    store.write("report.json", data)
    lines = [f"# QA run {board.run_id}", "", f"Execution mode: **{board.mode}**. LLM execution: **{data['llm_execution']}**.",
        "", f"Stop: {board.stop_reason}", "", f"Build: `{board.app_version}`; checkout commit: `{board.source_commit}`; source verified: {board.source_verified}.",
        "", "| Workflow | Rule | Outcome |", "| --- | --- | --- |"]
    lines += [f"| {c.workflow_id} | {c.rule_id} | {c.outcome.value} |" for c in board.assertions.values()]
    lines += ["", "## Specialist activity", "", "| Agent | Executed tools | Successful tools |", "| --- | --- | --- |"]
    lines += [f"| {role} | {activity['executed_actions']} | {activity['successful_actions']} |" for role, activity in data["agent_activity"].items()]
    review = board.agent_reviews.get("diagnosis", {})
    if review:
        lines += ["", f"Diagnosis evidence review: {'completed' if review.get('completed') else 'incomplete'}.",
                  "", review.get("conclusion", "Final review has not been submitted.")]
    for bug in bugs:
        diagnosis = bug["diagnosis"]
        lines += ["", f"## {bug['rule_id']}", "", f"Behavior: **{bug['behavioral_status']}**. Cause: **{bug['root_cause_status']}**.",
            "", "Expected: " + json.dumps(bug["assertion"]["expected"]), "", "Actual: " + json.dumps(bug["assertion"]["actual"]),
            "", diagnosis.get("explanation", "Diagnosis incomplete; exact cause remains unconfirmed.")]
        for replay in diagnosis.get("replay", []):
            lines += ["", f"Replay {replay['attempt']}: reproduced={replay['reproduced']}. Starting state: {replay['starting_state']}.", ""]
            lines += [f"{i}. {step}" for i, step in enumerate(replay["steps"], 1)]
        source = diagnosis.get("source")
        if source:
            lines += ["", f"Source: `{source['file']}` ({source['source_digest'][:16]}).", "", "```text"]
            lines += [f"{row['line']}: {row['text']}" for row in source["lines"]]
            lines += ["```"]
        lines += ["", "Targeted check: " + json.dumps(diagnosis.get("targeted_check")),
                  "", "Uncertainty: " + diagnosis.get("uncertainty", "Diagnosis incomplete"), "", "Evidence:", ""]
        refs = list(dict.fromkeys(bug["evidence_refs"] + diagnosis.get("evidence_refs", [])))
        lines += [f"- [{ref}]({ref})" for ref in refs]
    lines += ["", f"Cleanup: **{cleanup['outcome']}**; reservations released: {cleanup['reservations_released']}.",
              "", "See tool_events.jsonl, decisions.jsonl, shared_state.json and evidence directories for actual execution details.",
              "", *data["limitations"]]
    (store.folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    store.save()
    return data
