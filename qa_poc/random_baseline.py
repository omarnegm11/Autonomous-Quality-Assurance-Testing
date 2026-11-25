"""Seeded random valid actions, using the same app, identities, tools and budgets."""
import random
from .schemas import Decision, RULES


def run_random(tools, seed):
    rng = random.Random(seed)
    tools.store.write("random_settings.json", {"seed": seed, "selection": "random_valid_actions", "llm": False})
    while len(tools.board.assertions) < len(RULES):
        board = tools.board
        candidates = [
            ("ui_explorer", "observe_page", {}),
            ("ui_explorer", "take_screenshot", {}),
            ("auth_db_explorer", "inspect_session_status", {}),
        ]
        if "LOGOUT_REVOKES_SESSION" not in board.assertions:
            candidates.append(("auth_db_explorer", "verify_logout", {}))
        records = [r for r in board.records.values() if not r.get("deleted")]
        if not records and "TASK_DELETE" not in board.assertions:
            observation = tools.observe_page()
            if observation["login_visible"]:
                candidates.append(("ui_explorer", "select_test_identity", {"identity": "user_a"}))
            else:
                title = next((f["value"] for f in observation["fields"] if f["testid"] == "new-title"), "")
                candidates.append(("ui_explorer", "fill_field", {"testid": "new-title", "value": f"{board.run_id} random task"}))
                if title:
                    candidates.append(("ui_explorer", "click_element", {"testid": "create"}))
        elif records:
            record = records[0]
            task_id = record["id"]
            candidates += [
                ("ui_explorer", "reload_page", {}),
                ("ui_explorer", "verify_ui_isolation", {"task_id": task_id}),
            ]
            if "edited_title" not in record:
                candidates += [
                    ("api_explorer", "check_response_contract", {"task_id": task_id}),
                    ("auth_db_explorer", "verify_task_record", {"task_id": task_id}),
                ]
            if {"API_VALUES_MATCH", "DB_VALUES_MATCH"}.issubset(board.assertions):
                if task_id not in tools.probes:
                    candidates.append(("api_explorer", "call_allowed_endpoint", {"operation": "probe_owner", "task_id": task_id}))
                elif "OWNER_ONLY_EDIT" not in board.assertions:
                    candidates.append(("auth_db_explorer", "verify_ownership", {"task_id": task_id}))
                elif not any(f not in board.diagnoses for f in board.failures):
                    if "edited_title" not in record:
                        candidates.append(("api_explorer", "call_allowed_endpoint", {"operation": "edit", "task_id": task_id, "title": f"{board.run_id} random edit"}))
                    elif "TASK_EDIT" not in board.assertions:
                        candidates.append(("auth_db_explorer", "compare_saved_values", {"task_id": task_id}))
                    elif {"TASK_PERSISTENCE", "OWNER_UI_ISOLATION"}.issubset(board.assertions):
                        if "delete" not in tools.last_responses:
                            candidates.append(("api_explorer", "call_allowed_endpoint", {"operation": "delete", "task_id": task_id}))
                        else:
                            candidates.append(("api_explorer", "check_lifecycle", {"task_id": task_id}))
        for failure in board.failures:
            if failure not in board.diagnoses:
                candidates.append(("diagnosis", "submit_diagnosis", {"rule_id": failure,
                    "explanation": "Random action baseline observed this rule violation; source cause and reproduction are unconfirmed."}))
        role, tool, arguments = rng.choice(candidates)
        decision = Decision(agent=role, goal_id="RANDOM_BASELINE", tool=tool, arguments=arguments,
                            reason=f"Seeded random selection among {len(candidates)} permitted actions; no model")
        tools.store.append("decisions.jsonl", {"decision": decision.model_dump(), "selection": "random_valid_actions", "seed": seed})
        tools.execute(decision)
    board.stop_reason = "Random valid-action baseline completed; no model was called"
