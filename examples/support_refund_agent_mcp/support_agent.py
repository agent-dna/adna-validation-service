"""Support Agent: handles a refund request from the support lead. :8811

    python support_agent.py

Its LLM reads the request and the customer's email, and decides the refund and
the ticket. It asks the Payments Agent to refund, and opens the Jira ticket with
Atlassian's MCP server, through Kong. It holds no Stripe or Jira key.
"""

import json
import os
from pathlib import Path

import uvicorn
from agentdna.core import AgentDNA
from agentdna.http import RequestRefused, SignedRequest
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from llm import build_llm
from mcp_tools import ToolCallFailed, call_tool

load_dotenv()

PAYMENTS_AGENT_URL = "http://127.0.0.1:8812"
INBOX = Path(__file__).parent / "inbox"  # the customers' emails

PROMPT = """You are a support agent. Decide the refund and the Jira ticket.
Reply with JSON only: {{"payment_intent": "...", "amount": <in pence>, "summary": "<one line>"}}

The support lead asks:
{request}

The customer's email:
{email}
"""

# The agent's identity.
agent = AgentDNA(
    name=os.getenv("SUPPORT_AGENT_NAME", "Support Agent"),
    type="agent",
    api_key=os.environ["AGENTDNA_API_KEY"],
    provenance_layer_url=os.environ["PROVENANCE_LAYER_URL"],
    admin_server_url=os.environ["ADMIN_SERVER_URL"],
    agent_policy_file=Path(__file__).parent
    / "policies"
    / "support_agent.md",  # what it may do, for CBAC
    log_level="ERROR",
)

llm = build_llm()
app = FastAPI()


@app.post("/request")
async def handle(request: Request):
    # 1. Open the signed request: refused unless it's real and signed for this path
    #    without agentdna: data = await request.json()
    try:
        signed_request = SignedRequest(agent, await request.body(), str(request.url))
    except RequestRefused as error:
        return JSONResponse({"error": error.reason}, status_code=403)
    data = signed_request.json()

    # 2. The LLM decides the refund and the ticket, from the request and the customer's email
    email = (INBOX / f"{data['order']}.txt").read_text()
    reply = llm.invoke(PROMPT.format(request=data["request"], email=email)).text
    plan = json.loads(reply[reply.index("{") : reply.rindex("}") + 1])

    # 3. Ask the Payments Agent to refund. Agent to agent, signed.
    #    without agentdna: payments = httpx.Client(base_url=PAYMENTS_AGENT_URL, timeout=120)
    payments = signed_request.client(PAYMENTS_AGENT_URL, timeout=120)
    refund = payments.post(
        "/refund", json={"payment_intent": plan["payment_intent"], "amount": plan["amount"]}
    ).json()

    # 4. Open the Jira ticket with Atlassian's MCP server, through Kong. Signed; no Jira key.
    try:
        ticket = await call_tool(
            signed_request,
            "jira",
            "createJiraIssue",
            {
                "cloudId": os.environ["JIRA_URL"],  # your Jira site
                "projectKey": os.environ["JIRA_PROJECT"],
                "issueType": "Task",
                "summary": plan["summary"],
                "description": f"Order #{data['order']}. Refund: {refund}",
            },
        )
    except ToolCallFailed as failed:
        ticket = {"error": failed.error}

    # 5. Answer the support lead. Signed; the refund and the ticket are joined here.
    #    without agentdna: return JSONResponse({...})
    return JSONResponse(
        signed_request.respond({"refund": refund, "ticket": ticket.get("key", ticket)})
    )


if __name__ == "__main__":
    print(f"Support Agent   :8811  {agent.get_actor_id()}")
    uvicorn.run(app, host="127.0.0.1", port=8811, log_level="warning")
