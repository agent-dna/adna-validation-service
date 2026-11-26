"""The validation service for MCP: what it says to a gateway on /mcp/check and /mcp/answered."""

import asyncio
import json

from agentdna.error import (
    ADMIN_WHITELIST_CHECK_FAILED,
    GRADE_VERIFICATION_FAILED_HEAVY,
    HTTP_CALL_SIGNED_FOR_ANOTHER_PATH,
    RESULT_OK,
    TOOL_EXECUTION_FAILED,
)
from agentdna.http import build_http_call_payload
from agentdna.mcp.metadata import workflow_from_metadata, workflow_to_metadata
from agentdna.mcp.payload import build_mcp_tool_call_payload
from agentdna.mcp.relay.propagation import _descends_from
from agentdna.types import load_workflow
from starlette.testclient import TestClient

from adna_validation import validator
from adna_validation.validator import check, create_app, mcp_answered, mcp_check

REFUND = {"payment_intent": "pi_1", "amount": 4000}


def signed_tools_call(user, agent, name="refund", arguments=REFUND, sent_arguments=None):
    """The person asks, the agent's relay signs a tools/call: the JSON-RPC body a gateway receives.
    The workflow is the agent's signed call."""
    task = user.build(
        payload=build_http_call_payload("POST", "/request", '{"order": "4821"}', "application/json")
    )
    call = agent.build(
        payload=build_mcp_tool_call_payload(name, arguments), previous_workflows=task
    )
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments if sent_arguments is None else sent_arguments,
            "_meta": {"progressToken": 3, **workflow_to_metadata(call)},
        },
    }
    return json.dumps(body), call


def tool_result(text, is_error=False):
    """The MCP server's JSON-RPC answer."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
                "_meta": {"server": "kept"},
            },
        }
    )


def test_mcp_check_allows_and_says_what_to_forward(user, agent, second_agent, recorded):
    cbac_calls = []

    async def cbac(*args):
        cbac_calls.append(args)
        return "allow", 1000, "hash"

    body, call = signed_tools_call(user, agent)
    verdict = asyncio.run(mcp_check(second_agent, body, cbac))

    assert verdict["allowed"]
    forwarded = json.loads(verdict["body"])
    assert forwarded == {  # the agent's tools/call, without its signed workflow
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "refund", "arguments": REFUND, "_meta": {"progressToken": 3}},
    }
    allowed = load_workflow(json.loads(verdict["workflow"]))
    assert allowed.get_latest_envelope().from_ == second_agent.get_actor_id()
    assert allowed.get_latest_envelope().parent_envelope[0].hash == call.get_latest_envelope().hash
    assert verdict["run_id"] == call.get_root_envelope().run_id

    agent_id, service_id, tool, arguments, intent, description, intent_id = cbac_calls[0]
    assert (agent_id, service_id, tool, arguments, intent, intent_id) == (
        agent.get_actor_id(),
        second_agent.get_actor_id(),
        "refund",
        REFUND,
        '{"order": "4821"}',  # only the person's body, as on /check
        call.id,
    )
    assert recorded == []


def test_mcp_check_refusals_are_signed_and_recorded(
    user, agent, second_agent, recorded, monkeypatch
):
    async def deny(*args):
        return "deny", 7001, "cbac-hash"

    body, _ = signed_tools_call(user, agent)

    # CBAC says no: only its hash is recorded
    verdict = asyncio.run(mcp_check(second_agent, body, deny))
    assert (verdict["allowed"], verdict["status"], verdict["code"]) == (False, 403, 7001)
    assert recorded[-1].get_latest_envelope().payload == "cbac-hash"

    # Signed for £40, sent asking for £4,000: the arguments differ from what the agent signed
    changed, _ = signed_tools_call(
        user, agent, sent_arguments={"payment_intent": "pi_1", "amount": 400000}
    )
    verdict = asyncio.run(mcp_check(second_agent, changed))
    assert (verdict["status"], verdict["code"]) == (403, HTTP_CALL_SIGNED_FOR_ANOTHER_PATH)

    # Someone in the workflow not whitelisted
    monkeypatch.setattr(
        validator, "_not_whitelisted", lambda url, actor_ids: [agent.get_actor_id()]
    )
    verdict = asyncio.run(mcp_check(second_agent, body))
    assert verdict["code"] == ADMIN_WHITELIST_CHECK_FAILED

    assert len(recorded) == 3


def test_mcp_check_refuses_what_is_not_a_signed_tools_call_without_recording(
    user, agent, second_agent, recorded
):
    body, call = signed_tools_call(user, agent)
    unsigned = json.loads(body)
    del unsigned["params"]["_meta"]["agentdna"]
    tools_list = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    for not_signed in (json.dumps(unsigned), json.dumps(tools_list), "not json", "[]"):
        verdict = asyncio.run(mcp_check(second_agent, not_signed))
        assert (verdict["allowed"], verdict["status"], verdict["code"]) == (False, 400, 0)
    assert recorded == []


def test_mcp_check_verifies_every_signature_and_does_not_record_a_forged_workflow(
    user, agent, second_agent, recorded
):
    body, _ = signed_tools_call(user, agent)
    forged = body.replace("4821", "9999")  # the person's request, changed under their signature

    verdict = asyncio.run(mcp_check(second_agent, forged))

    assert (verdict["allowed"], verdict["status"], verdict["code"]) == (
        False,
        403,
        GRADE_VERIFICATION_FAILED_HEAVY,
    )
    assert recorded == []


def test_mcp_answered_signs_the_result_on_the_agents_call_so_the_relay_accepts_it(
    user, agent, second_agent, recorded
):
    body, call = signed_tools_call(user, agent)
    verdict = asyncio.run(mcp_check(second_agent, body))

    result = asyncio.run(mcp_answered(second_agent, verdict["workflow"], 200, tool_result("re_1")))

    answer = json.loads(result["body"])
    assert (answer["jsonrpc"], answer["id"]) == ("2.0", 7)
    assert answer["result"]["content"] == [{"type": "text", "text": "re_1"}]
    assert answer["result"]["_meta"]["server"] == "kept"

    # What the agent's relay checks: signed directly on its call, and genuine
    successor = workflow_from_metadata(answer["result"]["_meta"])
    assert successor is not None
    assert _descends_from(successor, call)
    assert agent.verify(successor) == RESULT_OK

    signed = json.loads(successor.get_latest_envelope().payload)
    assert signed == {
        "type": "mcp_result",
        "version": "1.0",
        "method": "tools/call",
        "status": "success",
        "result": {"content": [{"type": "text", "text": "re_1"}], "isError": False},
    }
    assert successor.get_latest_envelope().from_ == second_agent.get_actor_id()
    assert recorded == []  # a success is recorded when the person's code records the task


def test_mcp_answered_signs_and_records_a_tool_error(user, agent, second_agent, recorded):
    body, call = signed_tools_call(user, agent)
    verdict = asyncio.run(mcp_check(second_agent, body))

    result = asyncio.run(
        mcp_answered(second_agent, verdict["workflow"], 200, tool_result("card declined", True))
    )

    successor = workflow_from_metadata(json.loads(result["body"])["result"]["_meta"])
    assert successor is not None
    step = successor.get_latest_envelope()
    assert step.status_code == TOOL_EXECUTION_FAILED  # the relay takes it as the tool's answer
    assert json.loads(step.payload)["status"] == "error"
    assert _descends_from(successor, call)
    assert recorded == [successor]


def test_mcp_answered_records_an_http_or_jsonrpc_error_and_passes_it_through(
    user, agent, second_agent, recorded
):
    body, call = signed_tools_call(user, agent)
    allowed = asyncio.run(mcp_check(second_agent, body))["workflow"]
    jsonrpc_error = json.dumps(
        {"jsonrpc": "2.0", "id": 7, "error": {"code": -32602, "message": "Unknown tool: refund"}}
    )

    assert asyncio.run(mcp_answered(second_agent, allowed, 502, "bad gateway")) == {
        "recorded": True
    }
    assert asyncio.run(mcp_answered(second_agent, allowed, 200, jsonrpc_error)) == {
        "recorded": True
    }
    asyncio.run(mcp_answered(second_agent, allowed, 403, reason="no Stripe key for this run"))

    assert [step.get_latest_envelope().payload for step in recorded] == [
        "the MCP server answered 502",
        "Unknown tool: refund",
        "no Stripe key for this run",
    ]
    for step in recorded:
        assert step.get_latest_envelope().status_code == TOOL_EXECUTION_FAILED
        assert _descends_from(step, call)


def test_mcp_answered_signs_nothing_on_a_step_that_is_not_its_own_allowed_for_a_tools_call(
    user, agent, second_agent, recorded
):
    body, call = signed_tools_call(user, agent)
    not_the_services = agent.build(
        payload='{"allowed": "tools/call refund"}', previous_workflows=call
    )
    rest_allowed = asyncio.run(
        check(
            second_agent,
            "/x",
            json.dumps(
                {
                    "agentdna": {
                        "intent_workflow": agent.build(
                            payload=build_http_call_payload("POST", "/x", "{}", "application/json"),
                            previous_workflows=user.build(payload="y"),
                        ).serialize()
                    }
                }
            ),
        )
    )["workflow"]

    for workflow in (call.serialize(), not_the_services.serialize(), rest_allowed, "not json"):
        assert "error" in asyncio.run(mcp_answered(second_agent, workflow, 200, tool_result("x")))
        assert "error" in asyncio.run(mcp_answered(second_agent, workflow, 500))
    assert recorded == []


def test_mcp_answers_over_http(user, agent, second_agent, recorded):
    client = TestClient(create_app(second_agent))
    body, _ = signed_tools_call(user, agent)

    verdict = client.post("/validation/v1/mcp/check", json={"body": body}).json()
    assert verdict["allowed"]

    result = client.post(
        "/validation/v1/mcp/answered",
        json={"workflow": verdict["workflow"], "status": 200, "body": tool_result("re_1")},
    ).json()
    assert "agentdna" in json.loads(result["body"])["result"]["_meta"]


def test_mcp_writes_server_in_and_server_out_under_the_agents_call(
    user, agent, second_agent, recorded, evidence_rows
):
    body, call = signed_tools_call(user, agent)
    verdict = asyncio.run(mcp_check(second_agent, body, headers={}))  # the agent sent no key
    stripe = "http://stripe-mcp:9001/mcp"
    asyncio.run(
        mcp_answered(
            second_agent,
            verdict["workflow"],
            200,
            tool_result("re_1"),
            headers={"Authorization": "Bearer the-stripe-key"},
            destination=stripe,
        )
    )

    server_in, server_out = evidence_rows()
    the_agents_call = call.get_latest_envelope().signature
    assert (server_in["source"], server_in["request_id"]) == ("server_in", the_agents_call)
    assert (server_out["source"], server_out["request_id"]) == ("server_out", the_agents_call)
    assert (server_out["destination"], server_out["auth_status"]) == (stripe, "accepted")
    assert "the-stripe-key" not in json.dumps(server_out)
