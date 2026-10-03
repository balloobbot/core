"""Transport-independent LoRaWAN events and vendor device collections."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import logging
from typing import Protocol

from .payloads import Payload, StatusData, Uplink, UplinkData

__all__ = [
    "DeviceCollection",
    "DeviceDescriptor",
    "DeviceEvent",
    "DeviceEventData",
    "EventType",
    "StatusData",
    "Unsubscribe",
    "Uplink",
    "UplinkData",
    "notify",
    "subscribe",
]

_LOGGER = logging.getLogger(__name__)
type Unsubscribe = Callable[[], None]


class EventType(StrEnum):
    """Inventory and live activity types."""

    ADDED = "added"
    UPDATED = "updated"
    REMOVED = "removed"
    UPLINK = "up"
    JOIN = "join"
    STATUS = "status"
    ACK = "ack"
    TX_ACK = "txack"
    LOG = "log"
    LOCATION = "location"


@dataclass(frozen=True, slots=True)
class DeviceDescriptor:
    """Catalog identity and current inventory metadata."""

    network_id: str
    dev_eui: str
    name: str
    application_id: str
    profile_id: str
    catalog_model_id: str = ""
    vendor_id: int | None = None
    model: str = ""
    manufacturer: str = ""

    def __post_init__(self) -> None:
        """Normalize and validate device identity."""
        eui = self.dev_eui.replace(":", "").lower()
        if len(eui) != 16 or any(c not in "0123456789abcdef" for c in eui):
            raise ValueError("DevEUI must contain exactly eight hexadecimal bytes")
        object.__setattr__(self, "dev_eui", eui)


class DeviceEvent(Protocol):
    """Borrowed read-only event; payload remains owned by the producer."""

    @property
    def network_id(self) -> str: ...

    @property
    def dev_eui(self) -> str: ...

    @property
    def type(self) -> EventType: ...

    @property
    def received_at(self) -> datetime: ...

    @property
    def descriptor(self) -> DeviceDescriptor | None: ...

    @property
    def data(self) -> Payload | None: ...


@dataclass(frozen=True, slots=True)
class DeviceEventData:
    """Lightweight envelope for generated payloads and test fixtures."""

    network_id: str
    dev_eui: str
    type: EventType
    received_at: datetime
    descriptor: DeviceDescriptor | None = None
    data: Payload | None = None


class Device(Protocol):
    """Minimal model lifecycle required by a collection."""

    descriptor: DeviceDescriptor

    def handle_event(self, event: DeviceEvent) -> None: ...

    def close(self) -> None: ...


def notify[T](listeners: list[Callable[[T], None]], value: T) -> None:
    """Deliver synchronously, isolating consumers and allowing unsubscribe."""
    for listener in tuple(listeners):
        if listener not in listeners:
            continue
        try:
            listener(value)
        except Exception:
            _LOGGER.exception("LoRaWAN listener failed")


def subscribe[T](
    listeners: list[Callable[[T], None]], callback: Callable[[T], None]
) -> Unsubscribe:
    """Register a callback and return idempotent cleanup."""
    listeners.append(callback)

    def unsubscribe() -> None:
        if callback in listeners:
            listeners.remove(callback)

    return unsubscribe


class DeviceCollection[DeviceT: Device]:
    """Create vendor models from inventory, then route their live events."""

    def __init__(self, network_id: str) -> None:
        """Own models for exactly one logical network."""
        self.network_id = network_id
        self.devices: dict[str, DeviceT] = {}
        self._added: list[Callable[[DeviceT], None]] = []
        self._removed: list[Callable[[DeviceT], None]] = []
        self._closed = False

    def _create_device(self, descriptor: DeviceDescriptor) -> DeviceT | None:
        raise NotImplementedError

    def subscribe_device_added(
        self, callback: Callable[[DeviceT], None]
    ) -> Unsubscribe:
        """Report existing models immediately, then future additions."""
        stop = subscribe(self._added, callback)
        for device in tuple(self.devices.values()):
            notify([callback], device)
        return stop

    def subscribe_device_removed(
        self, callback: Callable[[DeviceT], None]
    ) -> Unsubscribe:
        """Listen for model retirement."""
        return subscribe(self._removed, callback)

    def _remove(self, dev_eui: str) -> None:
        if device := self.devices.pop(dev_eui, None):
            device.close()
            notify(self._removed, device)

    def handle_event(self, event: DeviceEvent) -> None:
        """Consume descriptors before activity, never infer a model from data."""
        if self._closed or event.network_id != self.network_id:
            return
        eui = event.dev_eui.replace(":", "").lower()
        if event.type == EventType.REMOVED:
            self._remove(eui)
            return
        device = self.devices.get(eui)
        if event.type in (EventType.ADDED, EventType.UPDATED):
            descriptor = event.descriptor
            if (
                descriptor is None
                or descriptor.network_id != self.network_id
                or descriptor.dev_eui != eui
            ):
                return
            if device and (
                device.descriptor.catalog_model_id,
                device.descriptor.vendor_id,
            ) != (descriptor.catalog_model_id, descriptor.vendor_id):
                self._remove(eui)
                device = None
            if device is None:
                if (device := self._create_device(descriptor)) is None:
                    return
                self.devices[eui] = device
                notify(self._added, device)
            else:
                device.descriptor = descriptor
        if device is not None:
            device.handle_event(event)

    def close(self) -> None:
        """Retire all models and listeners; repeated calls are harmless."""
        self._closed = True
        for eui in tuple(self.devices):
            self._remove(eui)
        self._added.clear()
        self._removed.clear()
