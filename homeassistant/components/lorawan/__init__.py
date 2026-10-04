"""LoRaWAN network provider using external ChirpStack servers."""

from collections.abc import Callable
from dataclasses import dataclass, field

from lorawan_connection import Connection, DeviceEvent, Downlink, EventType, Unsubscribe
from lorawan_connection.chirpstack import (
    AuthenticationError,
    ChirpStackConnection,
    ConnectionUnavailable,
)

from homeassistant import core
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import CONF_API_KEY, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_lorawan

from .const import (
    CONF_APPLICATION_IDS,
    CONF_ENDPOINT,
    CONF_NETWORK_ID,
    CONF_TENANT_ID,
    DOMAIN,
)
from .entity import LoRaWANEntity as LoRaWANEntity
from .websocket_api import async_register

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


class _ConsumerConnection:
    """Expose device operations without access to transport lifecycle."""

    __slots__ = ("_backend",)

    def __init__(self, backend: ChirpStackConnection) -> None:
        self._backend = backend

    async def async_subscribe(
        self,
        *,
        vendor_ids: frozenset[int],
        callback: Callable[[DeviceEvent], None],
    ) -> Unsubscribe:
        """Deliver existing devices and live events for the selected vendors."""
        return await self._backend.async_subscribe(
            vendor_ids=vendor_ids, callback=callback
        )

    def on_disconnect(self, callback: Callable[[], None]) -> Unsubscribe:
        """Listen for connection loss without controlling the connection."""
        return self._backend.on_disconnect(callback)

    async def async_send_downlink(self, downlink: Downlink) -> str:
        """Send a command to a device in this provider's selected applications."""
        return await self._backend.async_send_downlink(downlink)


@dataclass
class LoRaWANData:
    """Own provider inventory and vendor subscriptions."""

    connection: ChirpStackConnection
    integrations: dict[str, list[int]]
    consumer: Connection = field(init=False)
    unsubscribe_disconnect: Unsubscribe | None = None
    discovered: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        """Create the restricted consumer interface."""
        self.consumer = _ConsumerConnection(self.connection)


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
    return entry.runtime_data.consumer


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
    runtime = entry.runtime_data = LoRaWANData(
        connection, await async_get_lorawan(hass)
    )

    @core.callback
    def handle_event(event: DeviceEvent) -> None:
        if (
            event.type not in (EventType.ADDED, EventType.UPDATED)
            or (descriptor := event.descriptor) is None
        ):
            return
        for domain, vendors in runtime.integrations.items():
            if descriptor.vendor_id not in vendors or domain in runtime.discovered:
                continue
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

    async def async_stop(_: Event) -> None:
        """Close the transport before Home Assistant stops its event loop."""
        await async_unload_entry(hass, entry)

    try:
        await connection.async_connect()
        runtime.unsubscribe_disconnect = connection.on_disconnect(
            lambda: (
                None
                if hass.is_stopping
                else hass.config_entries.async_schedule_reload(entry.entry_id)
            )
        )
        entry.async_on_unload(runtime.unsubscribe_disconnect)
        entry.async_on_unload(
            await connection.async_subscribe(vendor_ids=None, callback=handle_event)
        )
    except AuthenticationError as error:
        raise ConfigEntryAuthFailed from error
    except ConnectionUnavailable as error:
        raise ConfigEntryNotReady from error
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LoRaWANConfigEntry) -> bool:
    """Close transport and notify dependent entries."""
    if entry.runtime_data.unsubscribe_disconnect is not None:
        entry.runtime_data.unsubscribe_disconnect()
    await entry.runtime_data.connection.close()
    return True
