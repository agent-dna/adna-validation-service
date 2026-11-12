# ADNA validation service

Gateways like Kong, Azure APIM, Apigee or AWS API Gateway can't run AgentDNA themselves. This service runs the AgentDNA checks for them: the gateway calls it around every call that carries a signed workflow, and does what it says.

```text
Alice ──▶ Assistant ──signed HTTP call──▶ Gateway + ADNA validation service ──plain call──▶ App
                                          checks the call, signs the app's answer
```

Agents sign their calls with [AgentDNA](https://github.com/agent-dna/agentdna)'s `SignedClient` (see "REST calls: signed HTTP and the ADNA gateway" in its README). This service is the gateway's side.

## Run it

```sh
pip install git+https://github.com/agent-dna/adna-validation-service
```

```py
from adna_validation import create_app

# gateway: the gateway's own AgentDNA identity. cbac_fn: the same CbacFn as the MCP middleware
app = create_app(gateway, cbac_fn)   # run with any ASGI server, e.g. uvicorn
```

Run it next to the gateway, reachable only by it.

## What the gateway calls

1. **Before forwarding:** `POST /check {"path", "body", "headers"}`. The service runs the whitelist (everyone who signed the workflow), GRADE and CBAC checks. On a yes, it returns the plain call to forward (`method`, `path`, `content_type`, `body`) and the person's `run_id`, so the gateway can fetch their key for the app. On a no, it returns the status and reason, and the refusal is recorded.
2. **After the app answers:** `POST /answered {"workflow", "status", "content_type", "body"}`. The service signs the app's answer, or records the error. The gateway sends back what it returns.

Calls without a workflow aren't AgentDNA's: the gateway handles them as it would without AgentDNA. If the service can't be reached, calls with a workflow are refused. The full request and answer formats are in [adna_validation/gate.py](adna_validation/gate.py).

With `AGENTDNA_AUTH_EVIDENCE` on, the service also writes credential rows: the key that reached the gateway (`server_in`), and the key the gateway sent to the app (`server_out`).

## Example

[examples/support_refund_agent](examples/support_refund_agent) does all of this with Kong: a support agent refunding a payment in Stripe and logging a ticket in Jira. Its `kong/` folder has the two Kong plugins: `agentdna-gate`, which calls this service, and `env-key`, which adds the app's key only to calls the service allowed.

## Development

```sh
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e . --group dev
ruff format --check adna_validation tests && ruff check adna_validation tests && pyright && pytest
```
