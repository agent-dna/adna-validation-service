import json
import secrets
from pathlib import Path

import httpx
import pytest
from agentdna.auth import httpobserver
from agentdna.auth.key import ENV_KEY
from agentdna.auth.sink import ENV_FILE
from agentdna.core import AgentDNA
from agentdna.types import (
    ACTOR_TYPE_AGENT,
    ACTOR_TYPE_USER,
)

from adna_validation import validator

ONLINE_PROVENANCE_NODE = "https://chain-connector-2-dev.rubix.net"
DUMMY_API_KEY = "31c1fd3f-5f2e-4dd1-9693-529de70c8347"
TEST_POLICY_FILE = Path(__file__).parent / "data" / "dummy_skills.md"
CONFIG_DIR = str(Path(__file__).parent / "temp")


@pytest.fixture
def user():
    return AgentDNA(
        name=f"user-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_USER,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        config_dir=CONFIG_DIR,
    )


@pytest.fixture
def agent():
    return AgentDNA(
        name=f"agent-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_AGENT,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        agent_policy_file=TEST_POLICY_FILE,
        config_dir=CONFIG_DIR,
    )


@pytest.fixture
def second_agent():
    return AgentDNA(
        name=f"agent-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_AGENT,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        agent_policy_file=TEST_POLICY_FILE,
        config_dir=CONFIG_DIR,
    )


@pytest.fixture
def recorded(second_agent, monkeypatch):
    """Whitelist says yes; recorded steps are kept here instead of sent to the provenance layer."""
    steps = []
    monkeypatch.setattr(validator, "_not_whitelisted", lambda url, actor_ids: [])
    monkeypatch.setattr(second_agent, "record", steps.append)
    return steps


@pytest.fixture
def evidence_rows(monkeypatch, tmp_path):
    """Credential rows go to a local file. Whatever recording patches is put back afterwards."""
    from requests.adapters import HTTPAdapter

    rows = tmp_path / "evidence.jsonl"
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(rows))
    monkeypatch.setattr(httpobserver, "_installed", False)
    saved = (httpx.Client.__init__, httpx.AsyncClient.__init__, HTTPAdapter.send)
    yield (
        lambda: (
            [json.loads(line) for line in rows.read_text().splitlines()] if rows.exists() else []
        )
    )
    httpx.Client.__init__, httpx.AsyncClient.__init__, HTTPAdapter.send = saved
