import json
import re
from pathlib import Path
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, select
from .schemas import Blackboard, now


def redact(value, secret_values=()):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"password|(?:^|_)token$|cookie|authorization|api_key|database_url", k, re.I)
                    else redact(v, secret_values)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret_values) for v in value]
    if isinstance(value, str):
        for secret in secret_values:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return re.sub(r"postgres(?:ql)?(?:\+psycopg)?://\S+", "[DATABASE_URL]", value)
    return value


class Store:
    def __init__(self, settings, board: Blackboard):
        self.board = board
        self.secret_values = tuple(v.get_secret_value() for v in (
            settings.test_user_a_password, settings.test_user_b_password, settings.llm_api_key,
            settings.api_auth_value))
        self.folder = settings.runs_dir / board.run_id
        self.folder.mkdir(parents=True)
        for name in ("screenshots", "api_evidence", "db_evidence", "source_evidence"):
            (self.folder / name).mkdir()
        self.engine = create_engine(settings.agent_state_database_url.get_secret_value())
        metadata = MetaData()
        self.table = Table("qa_runs", metadata, Column("run_id", String, primary_key=True),
                           Column("snapshot", Text, nullable=False))
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(self.table.insert().values(run_id=board.run_id, snapshot="{}"))

    def write(self, name, data):
        (self.folder / name).write_text(json.dumps(redact(data, self.secret_values), indent=2, default=str), encoding="utf-8")
        return name

    def append(self, name, data):
        with (self.folder / name).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(redact({"timestamp": now(), **data}, self.secret_values), default=str) + "\n")

    def save(self):
        data = redact(self.board.model_dump(mode="json"), self.secret_values)
        with self.engine.begin() as conn:
            conn.execute(self.table.update().where(self.table.c.run_id == self.board.run_id)
                         .values(snapshot=json.dumps(data)))
        self.write("shared_state.json", data)
