"""The standalone server: its settings, from .env alone, and its CBAC."""

import asyncio
from pathlib import Path

import pytest

from adna_validation import server

REQUIRED = """
AGENTDNA_API_KEY=key
PROVENANCE_LAYER_URL=https://provenance.example
ADMIN_SERVER_URL=https://admin.example
ADNA_VALIDATION_SERVICE_IDENTITY=Validation Service
"""


def env_file(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_no_env_file_no_start(tmp_path, monkeypatch):
    with pytest.raises(server.SettingsError, match="not found"):
        server.load_settings(tmp_path / ".env")

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="not found"):
        server.main()


@pytest.mark.parametrize("missing", server.REQUIRED)
def test_each_required_setting_must_be_set(tmp_path, missing):
    text = REQUIRED.replace(f"{missing}=", f"{missing}=\n# ")

    with pytest.raises(server.SettingsError, match=missing):
        server.load_settings(env_file(tmp_path, text))


def test_defaults(tmp_path):
    assert server.load_settings(env_file(tmp_path, REQUIRED)) == server.Settings(
        api_key="key",
        provenance_layer_url="https://provenance.example",
        admin_server_url="https://admin.example",
        adna_validation_service_identity="Validation Service",
        host="127.0.0.1",
        port=8810,
        cbac_url="https://cbac-service.agentdna.io",
        cbac_enabled=True,
    )


def test_every_setting_from_the_file(tmp_path):
    text = REQUIRED + (
        "HOST=0.0.0.0\nPORT=9000\nCBAC_URL=https://cbac.example\nCBAC_ENABLED=False\n"
    )

    assert server.load_settings(env_file(tmp_path, text)) == server.Settings(
        api_key="key",
        provenance_layer_url="https://provenance.example",
        admin_server_url="https://admin.example",
        adna_validation_service_identity="Validation Service",
        host="0.0.0.0",
        port=9000,
        cbac_url="https://cbac.example",
        cbac_enabled=False,
    )


def test_only_the_file_is_read_not_the_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("ADNA_VALIDATION_SERVICE_IDENTITY", "from-the-shell")

    settings = server.load_settings(env_file(tmp_path, REQUIRED))

    assert (settings.port, settings.adna_validation_service_identity) == (
        8810,
        "Validation Service",
    )


@pytest.mark.parametrize(
    "line, error", [("PORT=http", "PORT"), ("CBAC_ENABLED=maybe", "CBAC_ENABLED")]
)
def test_wrong_values_are_refused(tmp_path, line, error):
    with pytest.raises(server.SettingsError, match=error):
        server.load_settings(env_file(tmp_path, REQUIRED + line))


def test_the_sample_lists_every_setting():
    sample = (Path(__file__).parent.parent / ".env.sample").read_text()

    for key in (*server.REQUIRED, "HOST", "PORT", "CBAC_URL", "CBAC_ENABLED"):
        assert f"\n{key}=" in sample


def test_cbac_gets_the_validators_inputs_as_they_are(monkeypatch):
    asked = []

    async def authorize(*args, **kwargs):
        asked.append((args, kwargs))
        return "allow", 1000, "hash"

    monkeypatch.setattr(server, "authorize", authorize)
    ask_cbac = server.cbac_fn("https://cbac.example")

    verdict = asyncio.run(
        ask_cbac("agent", "service", "POST /stripe/v1/refunds", {"amount": 4000}, "£40", "", "wf")
    )

    assert verdict == ("allow", 1000, "hash")
    assert asked == [
        (
            ("agent", "service", "POST /stripe/v1/refunds", {"amount": 4000}, "£40", "", "wf"),
            {"callee_type": "tool", "cbac_url": "https://cbac.example"},
        )
    ]
