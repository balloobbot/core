"""Validate LoRaWAN manifest registrations and generated discovery data."""

import ast
import json
from pathlib import Path

import probatio
import pytest

from script.hassfest.lorawan import generate_and_validate
from script.hassfest.manifest import INTEGRATION_MANIFEST_SCHEMA
from script.hassfest.model import Config, Integration
from script.hassfest.quality_scale_validation import discovery


@pytest.mark.parametrize("vendors", [[], [-1], [65536], [744, 744], ["744"]])
def test_invalid_vendor_ids(vendors: list[int | str]) -> None:
    """Discovery identities must be a nonempty, unique list of 16-bit IDs."""
    manifest = json.loads(
        Path("homeassistant/components/sensecap/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    manifest["lorawan"] = vendors
    with pytest.raises(probatio.Invalid):
        INTEGRATION_MANIFEST_SCHEMA(manifest)


@pytest.mark.parametrize(
    ("missing", "platform_filename", "error_count"),
    [
        ("dependencies", "lorawan.py", 1),
        ("config_flow", "lorawan.py", 1),
        ("platform", "other.py", 1),
        (None, "lorawan.py", 0),
    ],
)
def test_registration_requirements(
    tmp_path: Path,
    config: Config,
    missing: str | None,
    platform_filename: str,
    error_count: int,
) -> None:
    """A discovered integration needs setup and library model declarations."""
    path = tmp_path / "vendor"
    path.mkdir()
    manifest = {
        "domain": "vendor",
        "lorawan": [744, 676],
        "dependencies": ["lorawan"],
        "config_flow": True,
    }
    (path / platform_filename).write_text("DEVICE_MODELS = ()\n")
    manifest.pop(missing, None)
    integration = Integration(path, _config=config, _manifest=manifest)
    source = generate_and_validate({"vendor": integration})
    assert ast.literal_eval(ast.parse(source).body[1].value) == {"vendor": [744, 676]}
    assert len(integration.errors) == error_count


def test_discovery_quality_rule(tmp_path: Path, config: Config) -> None:
    """The LoRaWAN manifest hook satisfies HA's discovery quality rule."""
    (tmp_path / "config_flow.py").write_text("# Config flow is validated separately.\n")
    integration = Integration(tmp_path, _config=config, _manifest={"lorawan": [744]})
    assert discovery.validate(config, integration, rules_done={"discovery"}) is None
