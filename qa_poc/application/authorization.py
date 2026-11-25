def may_edit(task_owner: str, current_user: str) -> bool:
    return task_owner == current_user
