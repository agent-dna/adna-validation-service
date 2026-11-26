"""Stripe's MCP server: one tool, refund. :8813, behind Kong at /mcp/stripe.

    python stripe_mcp.py

It holds no Stripe key. Kong adds the key to each call the AgentDNA validation
service allowed, and the tool passes it on to Stripe. A call that reaches it any
other way has no key, and Stripe refuses it.
"""

import httpx
from mcp.server.fastmcp import Context, FastMCP

# JSON answers, not an event stream: the validation service signs them
mcp = FastMCP("stripe", port=8813, stateless_http=True, json_response=True, log_level="WARNING")


@mcp.tool()
async def refund(payment_intent: str, amount: int, ctx: Context) -> dict:
    """Refund a Stripe payment. amount is in pence."""
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.stripe.com/v1/refunds",
            headers=key_from_kong(ctx),
            data={"payment_intent": payment_intent, "amount": amount},
        )
    if response.status_code != 200:
        raise RuntimeError(f"Stripe answered {response.status_code}: {response.text}")
    refunded = response.json()
    return {"refund": refunded["id"], "amount": refunded["amount"], "status": refunded["status"]}


def key_from_kong(ctx: Context) -> dict:
    """The key Kong put on this call, if it did."""
    request = ctx.request_context.request
    key = request.headers.get("authorization") if request else None
    return {"Authorization": key} if key else {}


if __name__ == "__main__":
    print("Stripe MCP      :8813")
    mcp.run(transport="streamable-http")
