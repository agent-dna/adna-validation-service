"""
The validation service as a standalone server:

    cp .env.sample .env        # then fill it in
    adna-validation-service

Every setting comes from the .env file in the current directory, and only from it.
Without the file the server does not start. .env.sample lists the settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentdna.mcp.server.types import CbacFn
from cbac import authorize
from dotenv import dotenv_values

ENV_FILE = ".env"
REQUIRED = (
    "AGENTDNA_API_KEY",
    "PROVENANCE_LAYER_URL",
    "ADMIN_SERVER_URL",
    "ADNA_VALIDATION_SERVICE_IDENTITY",
)


class SettingsError(Exception):
    """The .env file is missing, or a setting in it is missing or wrong."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    provenance_layer_url: str
    admin_server_url: str
    adna_validation_service_identity: str  # the name of the service's AgentDNA identity
    host: str = "127.0.0.1"
    port: int = 8810
    cbac_url: str = "https://cbac-service.agentdna.io"
    cbac_enabled: bool = True


def load_settings(env_file: Path) -> Settings:
    """The settings, from the .env file alone."""
    if not env_file.is_file():
        raise SettingsError(f"{env_file} not found. Copy .env.sample to .env and fill it in.")
    values = {key: (value or "").strip() for key, value in dotenv_values(env_file).items()}

    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise SettingsError(f"{env_file}: set {', '.join(missing)}")

    defaults = Settings(
        api_key="",
        provenance_layer_url="",
        admin_server_url="",
        adna_validation_service_identity="",
    )

    port = values.get("PORT") or str(defaults.port)
    if not port.isdigit():
        raise SettingsError(f"{env_file}: PORT must be a number, not {port!r}")

    cbac_enabled = (values.get("CBAC_ENABLED") or "true").lower()
    if cbac_enabled not in ("true", "false"):
        raise SettingsError(f"{env_file}: CBAC_ENABLED must be true or false, not {cbac_enabled!r}")

    return Settings(
        api_key=values["AGENTDNA_API_KEY"],
        provenance_layer_url=values["PROVENANCE_LAYER_URL"],
        admin_server_url=values["ADMIN_SERVER_URL"],
        adna_validation_service_identity=values["ADNA_VALIDATION_SERVICE_IDENTITY"],
        host=values.get("HOST") or defaults.host,
        port=int(port),
        cbac_url=values.get("CBAC_URL") or defaults.cbac_url,
        cbac_enabled=cbac_enabled == "true",
    )


def cbac_fn(cbac_url: str) -> CbacFn:
    """CBAC: the validator's inputs, passed to the CBAC service as they are."""

    async def ask_cbac(agent_id, service_id, call, arguments, intent, description, intent_id):
        return await authorize(
            agent_id,
            service_id,
            call,
            arguments,
            intent,
            description,
            intent_id,
            callee_type="tool",
            cbac_url=cbac_url,
        )

    return ask_cbac


def main() -> None:
    import uvicorn
    from agentdna.core import AgentDNA
    from dotenv import load_dotenv

    from adna_validation.validator import create_app

    env_file = Path(ENV_FILE)
    try:
        settings = load_settings(env_file)
    except SettingsError as error:
        raise SystemExit(f"adna-validation-service: {error}") from None
    load_dotenv(env_file, override=True)  # the SDK reads its own settings (AGENTDNA_AUTH_EVIDENCE)

    adna_validation_service_identity = AgentDNA(
        name=settings.adna_validation_service_identity,
        type="tool",
        api_key=settings.api_key,
        provenance_layer_url=settings.provenance_layer_url,
        admin_server_url=settings.admin_server_url,
    )
    app = create_app(
        adna_validation_service_identity,
        cbac_fn(settings.cbac_url) if settings.cbac_enabled else None,
    )

    print(
        f"Validation  {settings.host}:{settings.port}  {adna_validation_service_identity.get_actor_id()}",
        flush=True,
    )
    uvicorn.run(app, host=settings.host, port=settings.port)
