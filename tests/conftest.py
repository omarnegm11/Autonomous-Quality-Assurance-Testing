from pathlib import Path
from uuid import uuid4


def pytest_configure(config):
    # Keep test artifacts in this workspace; the host's shared pytest temp folder may have unrelated ACLs.
    if not config.option.basetemp:
        parent = Path(__file__).resolve().parents[1] / ".local" / "pytest"
        parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(parent / uuid4().hex)
