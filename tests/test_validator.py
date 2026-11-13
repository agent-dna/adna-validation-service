"""The validation service: what it says to a gateway on /check and /answered."""

import asyncio
import base64
import json

import httpx
import pytest
from agentdna.auth import httpobserver
from agentdna.auth.key import ENV_KEY
from agentdna.auth.sink import ENV_FILE
from agentdna.error import (
    ADMIN_WHITELIST_CHECK_FAILED,
    CALL_MISMATCH,
    GRADE_VERIFICATION_FAILED_HEAVY,
    TOOL_EXECUTION_FAILED,
)
from agentdna.http import (
    answer_from_body,
    build_http_answer_payload,
    build_http_call_payload,
    workflow_to_body,
)
from agentdna.types import load_workflow
from starlette.testclient import TestClient

from adna_validation import validator
from adna_validation.validator import answered, check, create_app

PDF = b"%PDF-1.7\n\xff\xfe\x00\x01 not text"


def signers_in_line(workflow):
    """Who signed each step, newest first. Fails if the workflow ever branches."""
    signers = []
    step = workflow.get_latest_envelope()
    while step:
        signers.append(step.from_)
        assert len(step.parent_envelope or []) <= 1
        step = step.parent_envelope[0] if step.parent_envelope else None
    return signers


def agent_call_root_run_id(verdict):
    """The run of the person's first step, read from the workflow the service signed."""
    return load_workflow(json.loads(verdict["workflow"])).get_root_envelope().run_id


def agent_call(user, agent, path, data):
    """The person asks, the agent signs a call: the body a gateway receives."""
    task = user.build(
        payload=build_http_call_payload(
            "POST", "/issues", '{"title": "Login bug"}', "application/json"
        )
    )
    call = build_http_call_payload("POST", path, json.dumps(data), "application/json")
    return json.dumps(workflow_to_body(agent.build(payload=call, previous_workflows=task)))


@pytest.fixture
def recorded(second_agent, monkeypatch):
    """Whitelist says yes; recorded steps are kept here instead of sent to the provenance layer."""
    steps = []
    monkeypatch.setattr(validator, "_not_whitelisted", lambda url, actor_ids: [])
    monkeypatch.setattr(second_agent, "record", steps.append)
    return steps


def test_validator_allows_and_says_what_to_forward(user, agent, second_agent, recorded):
    cbac_calls = []

    async def cbac(*args):
        cbac_calls.append(args)
        return "allow", 1000, "hash"

    verdict = asyncio.run(
        check(
            second_agent,
            "/github/issues",
            agent_call(user, agent, "/github/issues", {"title": "x"}),
            cbac,
        )
    )

    assert verdict["allowed"]
    assert (verdict["method"], verdict["path"], verdict["content_type"]) == (
        "POST",
        "/github/issues",
        "application/json",
    )
    assert json.loads(verdict["body"]) == {"title": "x"}
    assert verdict["run_id"] == agent_call_root_run_id(verdict)
    step = load_workflow(json.loads(verdict["workflow"]))
    assert signers_in_line(step) == [
        second_agent.get_actor_id(),
        agent.get_actor_id(),
        user.get_actor_id(),
    ]

    agent_id, gateway_id, tool, arguments, intent, description, intent_id = cbac_calls[0]
    assert (agent_id, gateway_id, tool, arguments) == (
        agent.get_actor_id(),
        second_agent.get_actor_id(),
        "POST /github/issues",
        {"title": "x"},
    )
    assert intent == '{"title": "Login bug"}'  # only the person's body, not their whole signed call
    assert intent_id == step.id
    assert recorded == []


def test_validator_refusals_are_signed_and_recorded(
    user, agent, second_agent, recorded, monkeypatch
):
    async def deny(*args):
        return "deny", 7001, "cbac-hash"

    body = agent_call(user, agent, "/github/issues", {"title": "x"})

    # CBAC says no: only its hash is recorded
    verdict = asyncio.run(check(second_agent, "/github/issues", body, deny))
    assert (verdict["allowed"], verdict["status"], verdict["code"]) == (False, 403, 7001)
    assert recorded[-1].get_latest_envelope().payload == "cbac-hash"

    # Signed for another path
    verdict = asyncio.run(check(second_agent, "/github/repos/delete", body))
    assert verdict["code"] == CALL_MISMATCH

    # Someone in the workflow not whitelisted: every signer is checked, the person too
    asked = []
    monkeypatch.setattr(
        validator,
        "_not_whitelisted",
        lambda url, actor_ids: asked.extend(actor_ids) or [agent.get_actor_id()],
    )
    verdict = asyncio.run(check(second_agent, "/github/issues", body))
    assert verdict["code"] == ADMIN_WHITELIST_CHECK_FAILED
    assert sorted(asked) == sorted([user.get_actor_id(), agent.get_actor_id()])

    assert len(recorded) == 3
    assert all(step.get_latest_envelope().from_ == second_agent.get_actor_id() for step in recorded)
    assert "workflow" not in verdict  # a refusal is on record; nothing travels back


def test_validator_refuses_a_body_with_no_workflow_without_recording(second_agent, recorded):
    verdict = asyncio.run(check(second_agent, "/github/issues", '{"title": "x"}'))

    assert (verdict["allowed"], verdict["status"]) == (False, 400)
    assert recorded == []


def test_validator_verifies_every_signature_and_does_not_record_a_forged_workflow(
    user, agent, second_agent, recorded
):
    body = agent_call(user, agent, "/github/issues", {"title": "x"}).replace(
        "Login bug", "Delete the repo"
    )
    verdict = asyncio.run(check(second_agent, "/github/issues", body))

    assert (verdict["allowed"], verdict["code"]) == (False, GRADE_VERIFICATION_FAILED_HEAVY)
    assert recorded == []


def test_validator_signs_the_apps_answer_on_its_own_allowed(user, agent, second_agent, recorded):
    verdict = asyncio.run(
        check(second_agent, "/github/issues", agent_call(user, agent, "/github/issues", {}))
    )

    result = asyncio.run(
        answered(second_agent, verdict["workflow"], 201, "application/json", '{"number": 12}')
    )

    workflow, status, content_type, answer = answer_from_body(json.dumps(result["body"]))
    assert (status, content_type, answer) == (
        201,
        "application/json",
        '{"number": 12}',
    )  # GitHub's real status
    gateway = second_agent.get_actor_id()
    assert signers_in_line(workflow) == [
        gateway,
        gateway,
        agent.get_actor_id(),
        user.get_actor_id(),
    ]
    assert recorded == []  # a success is recorded when the person's code records the task


def test_validator_forwards_and_signs_binary_bodies(user, agent, second_agent, recorded):
    cbac_calls = []

    async def cbac(*args):
        cbac_calls.append(args)
        return "allow", 1000, "hash"

    task = user.build(payload="Upload the report")
    call = build_http_call_payload(
        "POST", "/files", base64.b64encode(PDF).decode(), "application/pdf", "base64"
    )
    body = json.dumps(workflow_to_body(agent.build(payload=call, previous_workflows=task)))

    verdict = asyncio.run(check(second_agent, "/files", body, cbac))
    assert verdict["encoding"] == "base64" and base64.b64decode(verdict["body"]) == PDF
    assert cbac_calls[0][3] == {"body": f"<binary, {len(PDF)} bytes>"}

    # The gateway sends every app answer as base64: binary stays binary, text is signed as text
    result = asyncio.run(
        answered(
            second_agent,
            verdict["workflow"],
            200,
            "application/pdf",
            base64.b64encode(PDF).decode(),
            encoding="base64",
        )
    )
    assert answer_from_body(json.dumps(result["body"]))[3] == PDF

    result = asyncio.run(
        answered(
            second_agent,
            verdict["workflow"],
            200,
            "application/json",
            base64.b64encode(b'{"ok": true}').decode(),
            encoding="base64",
        )
    )
    signed = json.loads(
        load_workflow(json.loads(result["body"]["agentdna"]["intent_workflow"]))
        .get_latest_envelope()
        .payload
    )
    assert signed["body"] == '{"ok": true}' and "encoding" not in signed


def test_validator_records_an_app_error_on_its_own_allowed(user, agent, second_agent, recorded):
    verdict = asyncio.run(
        check(second_agent, "/github/issues", agent_call(user, agent, "/github/issues", {}))
    )

    assert asyncio.run(answered(second_agent, verdict["workflow"], 500, "text/plain", "boom")) == {
        "recorded": True
    }

    step = recorded[0].get_latest_envelope()
    assert (step.payload, step.status_code) == ("the app answered 500", TOOL_EXECUTION_FAILED)
    gateway = second_agent.get_actor_id()
    assert signers_in_line(recorded[0]) == [
        gateway,
        gateway,
        agent.get_actor_id(),
        user.get_actor_id(),
    ]


def test_validator_signs_nothing_on_a_step_that_is_not_its_own_allowed(
    user, agent, second_agent, recorded
):
    task = user.build(payload="y")
    not_the_gateways = agent.build(payload='{"allowed": "POST /x"}', previous_workflows=task)
    not_an_allowed = second_agent.build(
        payload=build_http_answer_payload("x", "text/plain", 200), previous_workflows=task
    )

    for workflow in (not_the_gateways, not_an_allowed):
        assert "error" in asyncio.run(
            answered(second_agent, workflow.serialize(), 201, "text/plain", "forged")
        )
        assert "error" in asyncio.run(answered(second_agent, workflow.serialize(), 500))
    assert recorded == []


def test_validator_answers_over_http(user, agent, second_agent, recorded):
    client = TestClient(create_app(second_agent))
    body = agent_call(user, agent, "/notes?day=mon", {})

    assert client.get("/health").json() == {"status": "ok"}

    verdict = client.post(
        "/validation/v1/check", json={"path": "/notes?day=mon", "body": body}
    ).json()
    assert verdict["allowed"]

    result = client.post(
        "/validation/v1/answered",
        json={
            "workflow": verdict["workflow"],
            "status": 200,
            "content_type": "application/json",
            "body": "{}",
        },
    ).json()
    assert list(result["body"]) == ["agentdna"]


def test_cbac_gets_plain_text_intent_as_it_is(user, agent):
    task = user.build(payload="Open an issue about the login bug")
    workflow = agent.build(
        payload=build_http_call_payload("POST", "/issues", "{}", "application/json"),
        previous_workflows=task,
    )

    assert validator._intent(workflow) == "Open an issue about the login bug"


# Credential rows


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


def test_the_validator_writes_server_in_and_server_out_under_the_agents_call(
    user, agent, second_agent, recorded, evidence_rows
):
    body = agent_call(user, agent, "/github/issues", {})
    verdict = asyncio.run(
        check(second_agent, "/github/issues", body, headers={})
    )  # the agent sent no key
    app = "https://api.github.com/repos/o/r/issues"
    asyncio.run(
        answered(
            second_agent,
            verdict["workflow"],
            201,
            "application/json",
            "{}",
            headers={"Authorization": "Bearer the-app-key"},
            destination=app,
        )
    )

    the_agents_call = (
        load_workflow(json.loads(verdict["workflow"])).get_latest_envelope().parent_envelope[0]
    )
    server_in, server_out = evidence_rows()
    assert (server_in["source"], server_in["request_id"]) == (
        "server_in",
        the_agents_call.signature,
    )
    assert (server_in["auth_method"], server_in["auth_status"]) == (
        "none",
        "unknown",
    )  # the service doesn't judge keys
    assert (server_out["source"], server_out["request_id"]) == (
        "server_out",
        the_agents_call.signature,
    )
    assert (server_out["destination"], server_out["auth_status"]) == (app, "accepted")
    assert server_out["credential_id"] and "the-app-key" not in json.dumps(
        server_out
    )  # a fingerprint, never the key


def test_the_app_refusing_the_key_shows_rejected_and_no_headers_means_no_rows(
    user, agent, second_agent, recorded, evidence_rows
):
    body = agent_call(user, agent, "/github/issues", {})

    verdict = asyncio.run(
        check(second_agent, "/github/issues", body)
    )  # no headers given: no server_in row
    asyncio.run(
        answered(
            second_agent,
            verdict["workflow"],
            401,
            "application/json",
            "{}",
            headers={"Authorization": "Bearer expired"},
            destination="https://api.github.com",
        )
    )

    [server_out] = evidence_rows()
    assert (server_out["source"], server_out["auth_status"]) == ("server_out", "rejected")


def test_the_gateway_stopping_a_call_after_the_yes_records_its_reason(
    user, agent, second_agent, recorded
):
    verdict = asyncio.run(
        check(second_agent, "/github/issues", agent_call(user, agent, "/github/issues", {}))
    )

    asyncio.run(
        answered(
            second_agent, verdict["workflow"], 403, reason="IBS: GitHub not allowed for this run"
        )
    )

    assert recorded[0].get_latest_envelope().payload == "IBS: GitHub not allowed for this run"
