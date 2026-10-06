"""Backend dependencies install when the shared distribution is already present."""

from importlib.metadata import PackageNotFoundError
from unittest.mock import call, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.requirements import _install_requirements_if_missing


@pytest.mark.parametrize(
    ("domain", "requirements"),
    [
        pytest.param("chirpstack", ["chirpstack-api==4.19.0"], id="chirpstack"),
        pytest.param(
            "the_things_stack", ["grpcio==1.83.1", "protobuf==7.36.0"], id="tts"
        ),
    ],
)
async def test_backend_dependencies(
    hass: HomeAssistant, domain: str, requirements: list[str]
) -> None:
    """Installing LoRaWAN first cannot hide the backend's missing packages."""
    integration = await async_get_integration(hass, domain)

    def installed_version(name: str) -> str:
        if name == "lorawan-connection":
            return "0.11.0"
        raise PackageNotFoundError(name)

    with (
        patch("homeassistant.util.package.version", side_effect=installed_version),
        patch(
            "homeassistant.util.package.install_package", return_value=True
        ) as install,
    ):
        _install_requirements_if_missing(integration.requirements, {})
    assert install.call_args_list == [call(requirement) for requirement in requirements]
