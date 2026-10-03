"""LoRaWAN network provider using external ChirpStack servers."""

from collections.abc import Callable
from dataclasses import dataclass, field

from lorawan_connection import (
    Connection,
    DeviceDescriptor,
    DeviceEvent,
    DeviceEventData,
    Downlink,
    DownlinkError,
    EventType,
    Unsubscribe,
    notify,
)
from lorawan_connection.chirpstack import (
    AuthenticationError,
    ChirpStackConnection,
    ConnectionUnavailable,
)

from homeassistant import core
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_APPLICATION_IDS,
    CONF_ENDPOINT,
    CONF_NETWORK_ID,
    CONF_TENANT_ID,
    DOMAIN,
)
from .websocket_api import async_register

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
# POC registration table. A manifest discovery matcher can replace this later.
VENDORS = {744: "sensecap", 676: "dragino"}


@dataclass
class Subscriber:
    """One vendor consumer."""

    vendor_ids: frozenset[int]
    callback: Callable[[DeviceEvent], None]
    on_disconnect: Callable[[], None]


@dataclass
class LoRaWANData:
    """Own provider inventory and vendor subscriptions."""

    connection: ChirpStackConnection
    devices: dict[str, DeviceDescriptor] = field(default_factory=dict)
    subscribers: list[Subscriber] = field(default_factory=list)
    discovered: set[str] = field(default_factory=set)


type LoRaWANConfigEntry = ConfigEntry[LoRaWANData]


@core.callback
def get_connection(hass: HomeAssistant, provider_entry_id: str) -> Connection:
    """Return the provider's connected network for a device collection."""
    entry: LoRaWANConfigEntry | None = hass.config_entries.async_get_entry(
        provider_entry_id
    )
    if (
        entry is None
        or entry.domain != DOMAIN
        or not hasattr(entry, "runtime_data")
        or not entry.runtime_data.connection.available
    ):
        raise ConnectionUnavailable("LoRaWAN provider is not connected")
    return entry.runtime_data.connection


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the admin inventory endpoint."""
    async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LoRaWANConfigEntry) -> bool:
    """Connect, build inventory, and discover vendor integrations."""
    connection = ChirpStackConnection(
        entry.data[CONF_ENDPOINT],
        entry.data[CONF_API_KEY],
        tenant_id=entry.data[CONF_TENANT_ID],
        application_ids=entry.data[CONF_APPLICATION_IDS],
        network_id=entry.data[CONF_NETWORK_ID],
    )
    runtime = entry.runtime_data = LoRaWANData(connection)

    @core.callback
    def handle_event(event: DeviceEvent) -> None:
        previous = runtime.devices.get(event.dev_eui)
        descriptor = event.descriptor or previous
        if descriptor is None:
            return
        if event.type == EventType.REMOVED:
            runtime.devices.pop(event.dev_eui, None)
        elif event.descriptor:
            runtime.devices[event.dev_eui] = descriptor
            if (
                domain := VENDORS.get(descriptor.vendor_id or 0)
            ) and domain not in runtime.discovered:
                runtime.discovered.add(domain)
                entry.async_create_task(
                    hass,
                    hass.config_entries.flow.async_init(
                        domain,
                        context={"source": SOURCE_INTEGRATION_DISCOVERY},
                        data={
                            "provider_entry_id": entry.entry_id,
                            CONF_NETWORK_ID: entry.data[CONF_NETWORK_ID],
                        },
                    ),
                    f"Discover {domain}",
                )
        for subscriber in tuple(runtime.subscribers):
            if (
                previous
                and previous.vendor_id in subscriber.vendor_ids
                and descriptor.vendor_id not in subscriber.vendor_ids
            ):
                notify(
                    [subscriber.callback],
                    DeviceEventData(
                        event.network_id,
                        event.dev_eui,
                        EventType.REMOVED,
                        event.received_at,
                        previous,
                    ),
                )
            elif descriptor.vendor_id in subscriber.vendor_ids:
                notify([subscriber.callback], event)

    @core.callback
    def disconnected(error: Exception) -> None:
        # The connection marks itself unavailable before invoking this callback.
        for subscriber in tuple(runtime.subscribers):
            subscriber.on_disconnect()
        runtime.subscribers.clear()
        hass.config_entries.async_schedule_reload(entry.entry_id)

    try:
        await connection.async_subscribe(handle_event, disconnected)
    except AuthenticationError as error:
        raise ConfigEntryAuthFailed from error
    except ConnectionUnavailable as error:
        raise ConfigEntryNotReady from error
    return True


async def async_subscribe(
    hass: HomeAssistant,
    provider_entry_id: str,
    vendor_ids: frozenset[int],
    callback: Callable[[DeviceEvent], None],
    on_disconnect: Callable[[], None],
) -> Unsubscribe:
    """Feed existing matching descriptors and then live vendor events."""
    entry: LoRaWANConfigEntry | None = hass.config_entries.async_get_entry(
        provider_entry_id
    )
    if (
        entry is None
        or entry.domain != DOMAIN
        or not hasattr(entry, "runtime_data")
        or not entry.runtime_data.connection.available
    ):
        raise ConnectionUnavailable("LoRaWAN provider is not connected")
    runtime = entry.runtime_data
    subscriber = Subscriber(vendor_ids, callback, on_disconnect)
    runtime.subscribers.append(subscriber)
    for descriptor in tuple(runtime.devices.values()):
        if descriptor.vendor_id in vendor_ids:
            notify(
                [callback],
                DeviceEventData(
                    descriptor.network_id,
                    descriptor.dev_eui,
                    EventType.ADDED,
                    dt_util.utcnow(),
                    descriptor,
                ),
            )

    def unsubscribe() -> None:
        if subscriber in runtime.subscribers:
            runtime.subscribers.remove(subscriber)

    return unsubscribe


async def async_send_downlink(
    hass: HomeAssistant, provider_entry_id: str, downlink: Downlink
) -> str:
    """Queue a command through the configured provider's selected inventory."""
    entry: LoRaWANConfigEntry | None = hass.config_entries.async_get_entry(
        provider_entry_id
    )
    if (
        entry is None
        or entry.domain != DOMAIN
        or not hasattr(entry, "runtime_data")
        or not entry.runtime_data.connection.available
    ):
        raise DownlinkError("LoRaWAN provider is not connected")
    if downlink.dev_eui not in entry.runtime_data.devices:
        raise DownlinkError("Device is not in this LoRaWAN provider")
    return await entry.runtime_data.connection.async_send_downlink(downlink)


async def async_unload_entry(hass: HomeAssistant, entry: LoRaWANConfigEntry) -> bool:
    """Close transport and notify dependent entries."""
    await entry.runtime_data.connection.close()
    for subscriber in tuple(entry.runtime_data.subscribers):
        subscriber.on_disconnect()
    entry.runtime_data.subscribers.clear()
    return True
