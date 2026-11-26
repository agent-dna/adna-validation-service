"""Payments Agent: refunds a Stripe payment when the Support Agent asks. :8812

    python payments_agent.py

It holds no Stripe key. It calls the Stripe MCP server's refund tool through
Kong, and Kong adds the key only after the AgentDNA validation service has
checked the call.
"""

import os
from pathlib import Path

import uvicorn
from agentdna.core import AgentDNA
from agentdna.http import RequestRefused, SignedRequest
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp_tools import ToolCallFailed, call_tool

load_dotenv()

# The agent's identity. Without agentdna: not needed
agent = AgentDNA(
    name=os.getenv("PAYMENTS_AGENT_NAME", "Payments Agent"),
    type="agent",
    api_key=os.environ["AGENTDNA_API_KEY"],
    provenance_layer_url=os.environ["PROVENANCE_LAYER_URL"],
    admin_server_url=os.environ["ADMIN_SERVER_URL"],
    agent_policy_file=Path(__file__).parent
    / "policies"
    / "payments_agent.md",  # what it may do, for CBAC
    log_level="ERROR",
)

app = FastAPI()


@app.post("/refund")
async def refund(request: Request):
    # 1. Open the signed request: refused unless it's real and signed for this path
    #    without agentdna: data = await request.json()
    try:
        signed_request = SignedRequest(agent, await request.body(), str(request.url))
    except RequestRefused as error:
        return JSONResponse({"error": error.reason}, status_code=403)
    data = signed_request.json()

    # 2. Refund with the Stripe MCP server's tool, through Kong. Signed; no Stripe key.
    try:
        refunded = await call_tool(
            signed_request,
            "stripe",
            "refund",
            {"payment_intent": data["payment_intent"], "amount": data["amount"]},
        )
    except ToolCallFailed as failed:
        # Not refunded: pass the error on. Whoever refused (the gateway, Stripe) has it on record.
        return JSONResponse({"error": failed.error}, status_code=failed.status)

    # 3. Answer the Support Agent. Signed, so the workflow carries on.
    #    without agentdna: return JSONResponse(refunded)
    return JSONResponse(signed_request.respond(refunded))


if __name__ == "__main__":
    print(f"Payments Agent  :8812  {agent.get_actor_id()}")
    uvicorn.run(app, host="127.0.0.1", port=8812, log_level="warning")
