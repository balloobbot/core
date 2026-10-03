"""SenseCAP devices on a LoRaWAN provider."""

from homeassistant.components import lorawan
from homeassistant.components.lorawan import ConnectionUnavailable
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from ._vendor.sensecap_lorawan import VENDOR_ID, SenseCapDeviceCollection

DOMAIN = "sensecap"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
type SenseCapConfigEntry = ConfigEntry[SenseCapDeviceCollection]
PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SenseCapConfigEntry) -> bool:
    """Forward all vendor events to one library collection."""
    try:
        connection = lorawan.get_connection(hass, entry.data["provider_entry_id"])
    except ConnectionUnavailable as error:
        raise ConfigEntryNotReady("LoRaWAN provider is not connected") from error
    devices = entry.runtime_data = SenseCapDeviceCollection(connection)

    @callback
    def disconnected() -> None:
        hass.config_entries.async_schedule_reload(entry.entry_id)

    try:
        stop = await lorawan.async_subscribe(
            hass,
            provider_entry_id=entry.data["provider_entry_id"],
            vendor_ids=frozenset({VENDOR_ID}),
            callback=devices.handle_event,
            on_disconnect=disconnected,
        )
    except ConnectionUnavailable as error:
        devices.close()
        raise ConfigEntryNotReady("LoRaWAN provider is not connected") from error
    entry.async_on_unload(stop)
    entry.async_on_unload(devices.close)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SenseCapConfigEntry) -> bool:
    """Unload entities; entry callbacks release collection and subscription."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
