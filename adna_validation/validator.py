"""
The validation service: the AgentDNA checks for a gateway that can't run them itself
(Azure APIM, Apigee, AWS API Gateway, Kong, Envoy). The gateway keeps routing
and forwarding; it sends each call here first and does what the answer says.

    app = create_app(adna_validation_service_identity, cbac_fn)        # run with any ASGI server, e.g. uvicorn

1. Before forwarding, the gateway sends:

    POST /validation/v1/check   {"path": "/github/repos/o/r/issues?x=1", "body": "<the raw request body>",
                   "headers": {<what the caller sent>}}                    # headers: for the credential row

and gets back a yes:

    {"allowed": true, "method": "POST", "path": "...", "content_type": "...", "body": "...", "encoding": "",
     "workflow": "...", "run_id": "..."}

Forward method, path, content_type and body to the app, never the request's
own, and keep workflow (it ends in the service's signed "allowed"). If encoding is
"base64", the body is binary: decode it before forwarding. Or a no:

    {"allowed": false, "status": 403, "reason": "...", "code": 1001}

Answer the caller with that status. The refusal is already signed and recorded.

2. When the app answers, whatever the answer, the gateway sends:

    POST /validation/v1/answered   {"workflow": "<the workflow from /check>", "status": 201, "content_type": "...", "body": "...",
                      "encoding": "base64", "headers": {<what the gateway sent the app>},
                      "destination": "<the app's URL>"}

encoding: send "base64" with the body base64-encoded, so binary answers (a PDF)
arrive intact, or leave it out for a text body.

On success the service signs the app's answer onto its "allowed" and returns
{"body": {...}}: send exactly that to the caller. On an error the service
signs and records it, and returns {"recorded": true}: pass the app's answer through.
If the gateway itself stops the call after the service's yes (no key, say), it
sends {"workflow", "status": 403, "reason": "..."} and the reason is recorded.

MCP: for a gateway in front of MCP servers, the same two calls, for a JSON-RPC
tools/call whose params._meta.agentdna holds a signed workflow. Other MCP
messages (initialize, tools/list, ...) and calls without a workflow aren't
AgentDNA's: the gateway handles them as it would without AgentDNA.

1. Before forwarding, the gateway sends:

    POST /validation/v1/mcp/check   {"body": "<the raw JSON-RPC request>", "headers": {<what the caller sent>}}

and gets back a yes:

    {"allowed": true, "body": "<the tools/call to forward>", "workflow": "...", "run_id": "..."}

Forward body to the MCP server (the agent's tools/call without its signed
workflow), and keep workflow. Or a no, as on /check: answer the caller with that status.

    {"allowed": false, "status": 403, "reason": "...", "code": 1001}

2. When the MCP server answers, whatever the answer, the gateway sends:

    POST /validation/v1/mcp/answered   {"workflow": "<the workflow from /mcp/check>", "status": 200,
                          "body": "<the raw JSON-RPC answer>", "headers": {...}, "destination": "..."}

The MCP server may answer with JSON or as an event stream (text/event-stream):
send its body as it came. On a result, the service signs it, with the tool's
output, onto the agent's call and returns {"body": "<the JSON-RPC answer>"},
always JSON: send exactly that to the caller, as application/json. On an HTTP
or JSON-RPC error it records the error and returns {"recorded": true}: pass the
answer through. A gateway stopping the call itself sends "reason", as on /answered.

GET /health answers {"status": "ok"} while the service is up.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from urllib.parse import urljoin

import requests
from agentdna.auth.httpobserver import SOURCE_SERVER_OUT, record_call
from agentdna.core import AgentDNA
from agentdna.error import (
    ADMIN_WHITELIST_CHECK_FAILED,
    ADMIN_WHITELIST_CHECK_SERVER_ERROR,
    HTTP_CALL_SIGNED_FOR_ANOTHER_PATH,
    MIDDLEWARE_EXECUTION_FAILED,
    RESULT_OK,
    TOOL_EXECUTION_FAILED,
)
from agentdna.http import (
    RequestRefused,
    SignedRequest,
    body_from_text,
    body_to_text,
    build_http_answer_payload,
    record_failure,
    workflow_from_body,
    workflow_to_body,
)
from agentdna.mcp.metadata import AGENTDNA_META_KEY, workflow_from_metadata, workflow_to_metadata
from agentdna.mcp.payload import build_mcp_tool_call_payload
from agentdna.mcp.server.observer import SOURCE_SERVER_IN
from agentdna.mcp.server.types import CbacFn
from agentdna.types import VERIFY_HEAVY, VERIFY_LIGHT, IntentWorkflow, load_workflow
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

API_PREFIX = "/validation/v1"


def create_app(adna_validation_service_identity, cbac_fn: CbacFn | None = None) -> Starlette:
    """The validation service. adna_validation_service_identity is the service's own AgentDNA identity."""

    async def check_endpoint(request: Request) -> JSONResponse:
        data = await request.json()
        return JSONResponse(
            await check(
                adna_validation_service_identity,
                data["path"],
                data["body"],
                cbac_fn,
                data.get("headers"),
            )
        )

    async def answered_endpoint(request: Request) -> JSONResponse:
        data = await request.json()
        return JSONResponse(
            await answered(
                adna_validation_service_identity,
                data["workflow"],
                data["status"],
                data.get("content_type", ""),
                data.get("body", ""),
                data.get("headers"),
                data.get("destination", ""),
                data.get("reason", ""),
                data.get("encoding", ""),
            )
        )

    async def mcp_check_endpoint(request: Request) -> JSONResponse:
        data = await request.json()
        return JSONResponse(
            await mcp_check(
                adna_validation_service_identity,
                data["body"],
                cbac_fn,
                data.get("headers"),
            )
        )

    async def mcp_answered_endpoint(request: Request) -> JSONResponse:
        data = await request.json()
        return JSONResponse(
            await mcp_answered(
                adna_validation_service_identity,
                data["workflow"],
                data["status"],
                data.get("body", ""),
                data.get("headers"),
                data.get("destination", ""),
                data.get("reason", ""),
            )
        )

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route(f"{API_PREFIX}/check", check_endpoint, methods=["POST"]),
            Route(f"{API_PREFIX}/answered", answered_endpoint, methods=["POST"]),
            Route(f"{API_PREFIX}/mcp/check", mcp_check_endpoint, methods=["POST"]),
            Route(f"{API_PREFIX}/mcp/answered", mcp_answered_endpoint, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ]
    )


async def check(
    adna_validation_service_identity: AgentDNA,
    path: str,
    body: str,
    cbac_fn: CbacFn | None = None,
    headers: dict | None = None,
) -> dict:
    """
    Run every check on one call. The first no stops it.
    headers: what the caller sent the gateway, for the credential row (server_in).
    """

    # Credential row: which key arrived with this call. The service doesn't judge
    # the key, so its status is unknown, as at an MCP server.
    opened = workflow_from_body(body)
    if opened and headers is not None:
        await asyncio.to_thread(
            record_call,
            adna_validation_service_identity,
            opened[0].get_latest_envelope(),
            SOURCE_SERVER_IN,
            headers,
            adna_validation_service_identity.get_actor_id(),
        )

    # Open, GRADE (every signature, back to the person), match (signed for this path?).
    # SignedRequest signs and records its own refusals (a forged workflow only logged).
    try:
        signed_request = await asyncio.to_thread(
            SignedRequest, adna_validation_service_identity, body, path, VERIFY_HEAVY
        )
    except RequestRefused as refused:
        status = 400 if refused.code == 0 else 403  # 0: no signed call at all
        return {"allowed": False, "status": status, "reason": refused.reason, "code": refused.code}

    workflow = signed_request.workflow

    # Whitelist and CBAC
    refused = await _whitelist_and_cbac(
        adna_validation_service_identity,
        workflow,
        f"{signed_request.method} {signed_request.path}",
        _arguments(signed_request.body),
        cbac_fn,
    )
    if refused:
        return refused

    # Yes: sign it, and say exactly what to forward
    allowed = await asyncio.to_thread(
        adna_validation_service_identity.build,
        payload=json.dumps({"allowed": f"{signed_request.method} {signed_request.path}"}),
        previous_workflows=workflow,
    )
    body, encoding = body_to_text(signed_request.body)
    return {
        "allowed": True,
        "method": signed_request.method,
        "path": signed_request.path,
        "content_type": signed_request.content_type,
        "body": body,
        "encoding": encoding,
        "workflow": allowed.serialize(),
        "run_id": workflow.get_root_envelope().run_id,  # the person's run: a gateway can fetch keys for it
    }


async def answered(
    adna_validation_service_identity: AgentDNA,
    workflow: str,
    status: int,
    content_type: str = "",
    body: str = "",
    headers: dict | None = None,
    destination: str = "",
    reason: str = "",
    encoding: str = "",
) -> dict:
    """
    The app answered. On success the app's answer is signed onto the service's
    "allowed", and the body to send back is returned. On an error, the error is
    signed and recorded. Only the service's own, genuine "allowed" is accepted, so
    nobody else can get an answer signed.

    headers and destination: what the gateway sent the app, and where, for the
    credential row (server_out). reason: why the call failed, when the gateway
    itself stopped it after the service's yes (no key for the app, say).
    encoding: "base64" if the gateway sent the body base64-encoded.
    """
    allowed = await _genuine_allowed(adna_validation_service_identity, workflow)
    if allowed is None:
        return {"error": 'not an "allowed" this service signed'}

    # Credential row: which key went to the app, and what the app said about it.
    # Filed under the agent's call, like the rows before it.
    if headers is not None:
        agent_call = (allowed.get_latest_envelope().parent_envelope or [])[0]
        await asyncio.to_thread(
            record_call,
            adna_validation_service_identity,
            agent_call,
            SOURCE_SERVER_OUT,
            headers,
            destination,
            status,
        )

    if status >= 400:
        await asyncio.to_thread(
            record_failure,
            adna_validation_service_identity,
            allowed,
            reason or f"the app answered {status}",
            TOOL_EXECUTION_FAILED,
        )
        return {"recorded": True}

    # Text is signed as text, a binary answer (a PDF) as base64
    body, encoding = body_to_text(body_from_text(body, encoding))
    answer = await asyncio.to_thread(
        adna_validation_service_identity.build,
        payload=build_http_answer_payload(body, content_type, status, encoding),
        previous_workflows=allowed,
    )
    return {"body": workflow_to_body(answer)}


async def mcp_check(
    adna_validation_service_identity: AgentDNA,
    body: str,
    cbac_fn: CbacFn | None = None,
    headers: dict | None = None,
) -> dict:
    """
    Run every check on one MCP tools/call, as check does on a REST call. The first no stops it.
    body: the JSON-RPC request the agent sent. headers: what it sent the gateway (server_in).
    """
    opened = _tools_call(body)
    if opened is None:
        return {
            "allowed": False,
            "status": 400,
            "reason": "no signed tools/call in the body",
            "code": 0,
        }
    request, workflow, name, arguments = opened

    # Credential row: which key arrived with this call, as on /check
    if headers is not None:
        await asyncio.to_thread(
            record_call,
            adna_validation_service_identity,
            workflow.get_latest_envelope(),
            SOURCE_SERVER_IN,
            headers,
            adna_validation_service_identity.get_actor_id(),
        )

    # GRADE: every signature, back to the person. A forged workflow is only logged (verify logs it).
    code = await asyncio.to_thread(adna_validation_service_identity.verify, workflow, VERIFY_HEAVY)
    if code != RESULT_OK:
        return {
            "allowed": False,
            "status": 403,
            "reason": "the signed workflow does not check out",
            "code": code,
        }

    # Match: the agent signed this tool, with these arguments
    if _signed(workflow) != json.loads(build_mcp_tool_call_payload(name, arguments)):
        return await _refuse(
            adna_validation_service_identity,
            workflow,
            f"signed for another tool call, not {name}",
            HTTP_CALL_SIGNED_FOR_ANOTHER_PATH,
        )

    # Whitelist and CBAC
    refused = await _whitelist_and_cbac(
        adna_validation_service_identity, workflow, name, arguments or {}, cbac_fn
    )
    if refused:
        return refused

    # Yes: sign it, and say exactly what to forward
    allowed = await asyncio.to_thread(
        adna_validation_service_identity.build,
        payload=json.dumps({"allowed": f"tools/call {name}"}),
        previous_workflows=workflow,
    )
    return {
        "allowed": True,
        "body": json.dumps(_without_workflow(request)),
        "workflow": allowed.serialize(),
        "run_id": workflow.get_root_envelope().run_id,  # the person's run: a gateway can fetch keys for it
    }


async def mcp_answered(
    adna_validation_service_identity: AgentDNA,
    workflow: str,
    status: int,
    body: str = "",
    headers: dict | None = None,
    destination: str = "",
    reason: str = "",
) -> dict:
    """
    The MCP server answered a tools/call. The answer, with the tool's output, is
    signed onto the agent's call: the agent's relay accepts nothing else. Only the
    service's own, genuine "allowed" for a tools/call is accepted, as on /answered.

    body: the JSON-RPC answer, as JSON or as an event stream; the signed answer is
    JSON either way. headers and destination: what the gateway sent the
    MCP server, and where (server_out). reason: why the call failed, when the
    gateway itself stopped it after the service's yes.
    """
    allowed = await _genuine_allowed(adna_validation_service_identity, workflow)
    call = _agents_tool_call(allowed) if allowed else None
    if call is None:
        return {"error": 'not an "allowed" this service signed for a tools/call'}

    # Credential row: which key went to the MCP server, filed under the agent's call
    if headers is not None:
        await asyncio.to_thread(
            record_call,
            adna_validation_service_identity,
            call.get_latest_envelope(),
            SOURCE_SERVER_OUT,
            headers,
            destination,
            status,
        )

    # No answer to sign: an HTTP error, a JSON-RPC error, or no JSON-RPC answer at all
    answer = _jsonrpc_answer(body)
    result = answer.get("result")
    if status >= 400 or not isinstance(result, dict):
        error = answer.get("error")
        if not reason and isinstance(error, dict) and error.get("message"):
            reason = str(error["message"])
        await asyncio.to_thread(
            record_failure,
            adna_validation_service_identity,
            call,
            reason or f"the MCP server answered {status}",
            TOOL_EXECUTION_FAILED,
        )
        return {"recorded": True}

    # The tool's result, signed. A tool error (isError) is signed and recorded as a
    # failure, as an AgentDNA MCP server does: the relay accepts it as the tool's answer.
    is_error = bool(result.get("isError", False))
    payload = json.dumps(
        {
            "type": "mcp_result",
            "version": "1.0",
            "method": "tools/call",
            "status": "error" if is_error else "success",
            "result": {key: value for key, value in result.items() if key != "_meta"},
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    if is_error:
        signed = await asyncio.to_thread(
            record_failure, adna_validation_service_identity, call, payload, TOOL_EXECUTION_FAILED
        )
    else:
        signed = await asyncio.to_thread(
            adna_validation_service_identity.build, payload=payload, previous_workflows=call
        )

    meta = result.get("_meta")
    meta = dict(meta) if isinstance(meta, dict) else {}
    agentdna_meta = meta.get(AGENTDNA_META_KEY)
    agentdna_meta = dict(agentdna_meta) if isinstance(agentdna_meta, dict) else {}
    agentdna_meta.update(workflow_to_metadata(signed)[AGENTDNA_META_KEY])
    meta[AGENTDNA_META_KEY] = agentdna_meta
    return {"body": json.dumps({**answer, "result": {**result, "_meta": meta}})}


async def _whitelist_and_cbac(
    adna_validation_service_identity: AgentDNA,
    workflow: IntentWorkflow,
    call: str,
    arguments: dict,
    cbac_fn: CbacFn | None,
) -> dict | None:
    """The whitelist and CBAC checks, the same for a REST call and a tools/call. A refusal, or None."""
    agent_id = workflow.get_latest_envelope().from_

    # Whitelist: an admin approved everyone who signed the workflow (as the MCP server checks)
    try:
        refused = await asyncio.to_thread(
            _not_whitelisted,
            adna_validation_service_identity.agentdna_admin_url,
            workflow.list_actors(),
        )
    except Exception as error:
        return await _refuse(
            adna_validation_service_identity,
            workflow,
            f"could not check the whitelist: {error}",
            ADMIN_WHITELIST_CHECK_SERVER_ERROR,
        )
    if refused:
        return await _refuse(
            adna_validation_service_identity,
            workflow,
            f"not whitelisted: {', '.join(refused)}",
            ADMIN_WHITELIST_CHECK_FAILED,
        )

    # CBAC: the call fits what the person asked (the same interface as the MCP server)
    if cbac_fn:
        try:
            decision, status, message_hash = await cbac_fn(
                agent_id,
                adna_validation_service_identity.get_actor_id(),
                call,
                arguments,
                _intent(workflow),
                "",
                workflow.id,
            )
        except Exception as error:
            return await _refuse(
                adna_validation_service_identity,
                workflow,
                f"could not run CBAC: {error}",
                MIDDLEWARE_EXECUTION_FAILED,
            )
        if decision.lower() != "allow":
            return await _refuse(
                adna_validation_service_identity,
                workflow,
                f"CBAC said {decision}",
                status,
                record_payload=message_hash,
            )
    return None


async def _genuine_allowed(
    adna_validation_service_identity: AgentDNA, workflow: str
) -> IntentWorkflow | None:
    """The workflow, if it ends in an "allowed" this service signed, and checks out. Else None."""
    try:
        allowed = load_workflow(json.loads(workflow))
        tip = allowed.get_latest_envelope()
        genuine = (
            tip.from_ == adna_validation_service_identity.get_actor_id()
            and "allowed" in json.loads(tip.payload)
        )
    except Exception:
        return None
    if (
        not genuine
        or await asyncio.to_thread(adna_validation_service_identity.verify, allowed, VERIFY_LIGHT)
        != RESULT_OK
    ):
        return None
    return allowed


def _tools_call(body: str) -> tuple[dict, IntentWorkflow, str, dict | None] | None:
    """The JSON-RPC tools/call, its signed workflow, the tool and its arguments. None if any is missing."""
    try:
        request = json.loads(body)
        params = request["params"]
        name = params["name"]
        arguments = params.get("arguments")
        if request.get("method") != "tools/call" or not isinstance(name, str):
            return None
        if arguments is not None and not isinstance(arguments, dict):
            return None
        workflow = workflow_from_metadata(params.get("_meta"))
    except Exception:
        return None
    if workflow is None or workflow.envelope is None:
        return None
    return request, workflow, name, arguments


def _without_workflow(request: dict) -> dict:
    """The tools/call to forward: the agent's own, without the signed workflow."""
    params = dict(request["params"])
    meta = {key: value for key, value in params["_meta"].items() if key != AGENTDNA_META_KEY}
    if meta:
        params["_meta"] = meta
    else:
        del params["_meta"]
    return {**request, "params": params}


def _agents_tool_call(allowed: IntentWorkflow) -> IntentWorkflow | None:
    """The agent's tools/call under the service's "allowed", as a workflow of its own. None if
    the step under it is not a tools/call."""
    parents = allowed.get_latest_envelope().parent_envelope or []
    if not parents:
        return None
    call = replace(allowed, envelope=parents[0])
    signed = _signed(call)
    if not isinstance(signed, dict) or signed.get("method") != "tools/call":
        return None
    return call


def _signed(workflow: IntentWorkflow) -> object:
    """What the newest step signed, read as JSON. None if it isn't JSON."""
    return _json(workflow.get_latest_envelope().payload)


def _json(text: str) -> object:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _jsonrpc_answer(body: str) -> dict:
    """
    The JSON-RPC answer in an MCP server's reply: the body itself when it's JSON,
    or, in an event stream, the last message that answers (has a result or an
    error); notifications sent before it are skipped. {} if there's none.
    """
    answer = _json(body)
    if isinstance(answer, dict):
        return answer
    for message in reversed(_event_stream_messages(body)):
        if isinstance(message, dict) and ("result" in message or "error" in message):
            return message
    return {}


def _event_stream_messages(body: str) -> list:
    """The data of each event in an event stream, read as JSON (None if it isn't)."""
    messages, data = [], []
    for line in [*(body or "").splitlines(), ""]:  # a blank line ends an event
        if not line:
            if data:
                messages.append(_json("\n".join(data)))
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    return messages


async def _refuse(
    adna_validation_service_identity: AgentDNA,
    workflow,
    reason: str,
    code: int,
    record_payload: str | None = None,
) -> dict:
    """Sign the refusal onto the workflow and record it. CBAC's reason may hold
    personal data, so only its hash is recorded, as the MCP server does."""
    await asyncio.to_thread(
        record_failure, adna_validation_service_identity, workflow, record_payload or reason, code
    )
    return {"allowed": False, "status": 403, "reason": reason, "code": code}


def _not_whitelisted(admin_url: str, actor_ids: list[str]) -> list[str]:
    """The actors the admin server has not whitelisted: the same request as the MCP server's."""
    response = requests.post(
        urljoin(admin_url, "/agent-admin/v1/whitelist/check"), json={"dids": actor_ids}, timeout=30
    )
    response.raise_for_status()
    answer = response.json()
    if not answer["status"]:
        raise RuntimeError("the admin server could not check the whitelist")
    return answer["data"]["blacklist"]


def _intent(workflow) -> str:
    """What the person asked: the body of their signed call, or their plain text."""
    payload = workflow.get_root_envelope().payload
    try:
        return json.loads(payload)["body"]
    except Exception:
        return payload


def _arguments(body: str | bytes) -> dict:
    """The signed body as CBAC's tool arguments: the JSON object, the text as is, or a binary body's size."""
    if isinstance(body, bytes):
        return {"body": f"<binary, {len(body)} bytes>"}
    try:
        value = json.loads(body)
    except ValueError:
        value = None
    return value if isinstance(value, dict) else {"body": body}
