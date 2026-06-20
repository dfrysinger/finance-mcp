import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_accounts.json"


@pytest.fixture
def raw_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def normalized(raw_data):
    from finance_mcp import normalize

    return normalize.normalize(raw_data)
