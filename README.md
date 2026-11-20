# ADNA validation service

API gateways like Kong, Azure APIM, Apigee or AWS API Gateway can't run AgentDNA themselves. The ADNA validation service is a standalone server that runs the AgentDNA checks for them: the gateway calls its API around every call that carries a signed workflow, and does what it says.

```text
Alice ──▶ Assistant ──signed HTTP call──▶ Gateway ──plain call──▶ App
                                            │  ▲
                              before / after│  │allowed? signed answer
                                            ▼  │
                                  ADNA validation service
                                  checks the call, signs the app's answer
```

Agents sign their calls with [AgentDNA](https://github.com/agent-dna/agentdna)'s `SignedClient` (see "REST calls: signed HTTP and the ADNA gateway" in its README). The validation service checks those calls, and signs the apps' answers, with its own AgentDNA identity.

## Run it

```sh
pip install git+https://github.com/agent-dna/adna-validation-service
cp .env.sample .env                # then fill it in
adna-validation-service
```

Every setting comes from the `.env` file in the current directory, and only from it: without the file the server doesn't start. [.env.sample](.env.sample) lists them:

| Setting | |
|---|---|
| `AGENTDNA_API_KEY`, `PROVENANCE_LAYER_URL`, `ADMIN_SERVER_URL` | Required. |
| `ADNA_VALIDATION_SERVICE_IDENTITY` | Required. The name of the service's AgentDNA identity. |
| `HOST`, `PORT` | Where the server listens. Default `127.0.0.1`, `8810`. |
| `CBAC_URL`, `CBAC_ENABLED` | The CBAC service, and whether to ask it. Default `https://cbac-service.agentdna.io`, `true`. |

At start the server prints its AgentDNA ID: whitelist it on the admin server. Run it next to the gateway, reachable only by it.

## What the gateway calls

1. **Before forwarding:** `POST /validation/v1/check {"path", "body", "headers"}`. The service runs the whitelist (everyone who signed the workflow), GRADE and CBAC checks. On a yes, it returns the plain call to forward (`method`, `path`, `content_type`, `body`) and the person's `run_id`, so the gateway can fetch their key for the app. On a no, it returns the status and reason, and the refusal is recorded.
2. **After the app answers:** `POST /validation/v1/answered {"workflow", "status", "content_type", "body"}`. The service signs the app's answer, or records the error. The gateway sends back what it returns.

Calls without a workflow aren't AgentDNA's: the gateway handles them as it would without AgentDNA. If the service can't be reached, calls with a workflow are refused. The full request and answer formats are in [adna_validation/validator.py](adna_validation/validator.py). `GET /health` answers `{"status": "ok"}` while the service is up.

With `AGENTDNA_AUTH_EVIDENCE` on, the service also writes credential rows: the key that reached the gateway (`server_in`), and the key the gateway sent to the app (`server_out`).

## Example

[examples/support_refund_agent](examples/support_refund_agent) does all of this with Kong: a support agent refunding a payment in Stripe and logging a ticket in Jira. Its `start.sh` starts the validation service, and its `kong/` folder has the two Kong plugins: `agentdna-validation`, which calls the service's API, and `env-key`, which adds the app's key only to calls the service allowed.

## Development

```sh
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e . --group dev
ruff format --check adna_validation tests && ruff check adna_validation tests && pyright && pytest
```
