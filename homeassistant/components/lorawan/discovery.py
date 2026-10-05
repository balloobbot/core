"""Resolve model support from vendor integration platforms."""

from collections.abc import Sequence
from typing import Protocol, cast

from lorawan_connection import Device, DeviceDescriptor

from homeassistant.core import HomeAssistant
from homeassistant.requirements import async_get_integration_with_requirements

from .const import DOMAIN


class LoRaWANPlatform(Protocol):
    """Device model declarations supplied by a vendor integration."""

    DEVICE_MODELS: Sequence[type[Device]]


async def async_unsupported_reason(
    hass: HomeAssistant,
    device: DeviceDescriptor,
    integrations: dict[str, list[tuple[str, int | str]]],
) -> str | None:
    """Explain support without requiring a configured vendor entry."""
    if device.brand_id is None or not device.model_id:
        return "no_catalog_identity"
    domains = [
        domain
        for domain, vendors in integrations.items()
        if (device.stack, device.brand_id) in vendors
    ]
    if not domains:
        return "no_vendor_integration"
    for domain in domains:
        integration = await async_get_integration_with_requirements(hass, domain)
        platform = cast(LoRaWANPlatform, await integration.async_get_platform(DOMAIN))
        if any(
            model.identifiers.get(device.stack) == (device.brand_id, device.model_id)
            for model in platform.DEVICE_MODELS
        ):
            return None
    return "model_not_supported"
