"""The agents' MCP tool calls: signed by AgentDNA's relay, sent through Kong.

    output = await call_tool(signed_request, "stripe", "refund", {...})

The relay runs inside the agent's process. It signs each tools/call onto the
request's workflow, and checks that the answer is signed onto it in turn. Kong
checks the call with the validation service before the MCP server sees it.
"""

import json

import httpx
from agentdna.http import SignedRequest
from agentdna.mcp.relay import ChainTerminated, ProvenanceRelay
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

KONG_URL = "http://127.0.0.1:8880"

# One relay per process. Each tool call opens its own session on it.
RELAY = ProvenanceRelay({"stripe": f"{KONG_URL}/mcp/stripe", "jira": f"{KONG_URL}/mcp/jira"})


class ToolCallFailed(Exception):
    """Kong refused the call (the validation service recorded why), or the tool failed."""

    def __init__(self, status: int, error):
        super().__init__(error)
        self.status = status
        self.error = error


async def call_tool(signed_request: SignedRequest, server: str, tool: str, arguments: dict):
    """
    Call one MCP tool, signed onto where the request has got to, and move the
    request on to the tool's signed answer. Returns the tool's output.
    """
    # Without agentdna: call the tool at f"{KONG_URL}/mcp/{server}" directly
    try:
        async with RELAY.session(signed_request.agent, signed_request.frontier.get()) as session:
            result, refused = await _call(session.url(server), tool, arguments)
    except ChainTerminated as ended:  # the chain ended, and the reason is on record
        raise ToolCallFailed(ended.status_code or 403, ended.reason) from None
    for workflow in session.workflows:
        signed_request.frontier.advance(workflow)

    if refused is not None:
        raise ToolCallFailed(refused.status_code, _error(refused))
    text = "".join(part.text for part in result.content if part.type == "text")
    if result.isError:
        raise ToolCallFailed(502, text)
    return json.loads(text)


async def _call(url: str, tool: str, arguments: dict):
    """A plain MCP client call. Returns (the result, None), or (None, Kong's refusal)."""

    async def keep_error_body(response: httpx.Response):
        if response.status_code >= 400:
            await response.aread()  # so the refusal's reason can be read

    http_client = httpx.AsyncClient(timeout=120, event_hooks={"response": [keep_error_body]})
    refused = None
    try:
        async with (
            http_client,
            streamable_http_client(url, http_client=http_client) as (
                read,
                write,
                _,
            ),
        ):
            async with ClientSession(read, write) as mcp:
                await mcp.initialize()
                return await mcp.call_tool(tool, arguments), None
    except* httpx.HTTPStatusError as errors:  # the MCP client raises an HTTP error in a group
        refused = _first(errors).response
    return None, refused


def _first(errors: BaseExceptionGroup) -> httpx.HTTPStatusError:
    error = errors.exceptions[0]
    return _first(error) if isinstance(error, BaseExceptionGroup) else error  # type: ignore[return-value]


def _error(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return response.text
