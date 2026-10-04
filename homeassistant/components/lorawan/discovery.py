"""POC vendor registrations shared by discovery and device support reporting."""

from dataclasses import dataclass

from lorawan_connection import DeviceDescriptor


@dataclass(frozen=True)
class VendorIntegration:
    """Declare an integration and the catalog models it supports."""

    domain: str
    models: frozenset[str]


# Replace this POC table with integration discovery metadata before upstreaming.
VENDORS = {
    744: VendorIntegration(
        "sensecap", frozenset({"fc455aa2-01cf-492b-9359-a5d8c9a0e1b3"})
    ),
    676: VendorIntegration(
        "dragino", frozenset({"cb0a7bef-eaa0-4c61-a0b6-ce33e6ecbc4f"})
    ),
}


def unsupported_reason(device: DeviceDescriptor) -> str | None:
    """Explain missing support independently of vendor config entry setup."""
    if device.vendor_id is None or not device.catalog_model_id:
        return "no_catalog_identity"
    if (integration := VENDORS.get(device.vendor_id)) is None:
        return "no_vendor_integration"
    if device.catalog_model_id not in integration.models:
        return "model_not_supported"
    return None
