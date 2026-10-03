# LoRaWAN proof of concept

This worktree adds `lorawan` and `sensecap` integrations. It connects to an
existing ChirpStack 4.19 server and discovers provisioned devices. The first
SenseCAP model is S2101, with temperature and humidity sensors. Provisioning,
BLE, gateway setup, a managed HA app, and a custom frontend are later phases.

## Get the POC branch

```sh
git clone --branch lorawan-poc --single-branch https://github.com/balloobbot/core.git core-lorawan
cd core-lorawan
```

The [implementation findings](https://gisthost.github.io/?c66c965ba7be32f9b0b64ff5c0ce0241#findings)
and [run instructions](https://gisthost.github.io/?c66c965ba7be32f9b0b64ff5c0ce0241#running)
are also available on the proposal website.

## Try it in Home Assistant

1. Run `script/setup` in this worktree. Install `chirpstack-api==4.19.0` with
   `uv pip install chirpstack-api==4.19.0` if running tests directly. HA installs
   it from the LoRaWAN manifest when setting up the integration.
2. On ChirpStack, import the current device-profile catalog. Assign the **global
   SenseCAP S2101 catalog profile** for the device's radio region. A custom
   tenant profile with the same name is not enough: ChirpStack ignores the
   `device_id` field when creating a custom profile.
3. Enable ChirpStack's per-device event log (enabled by default). The current
   adapter requires the internal `StreamDeviceEvents` gRPC endpoint.
4. Add integration → LoRaWAN. Enter `https://host:port` and an API key. For an
   unencrypted local server, explicitly use `http://host:port`. TLS uses the
   platform's trusted roots; there is no automatic downgrade or insecure TLS.
5. Select a tenant and applications. Global keys can list tenants. Tenant-scoped
   keys require their tenant UUID. ChirpStack reports insufficient listing scope
   as UNAUTHENTICATED, so the flow validates the key after selecting a tenant. Full-access and read-only keys both work.
6. Confirm the discovered SenseCAP integration. It creates one collection for
   the network, and adds further supported devices automatically.

The admin-only `lorawan/devices` websocket command takes `entry_id` and returns
current descriptors and availability. It exposes no credentials. The regular HA
integration/device/entity pages show the provider, collection, and sensors.

## Source layout

- `homeassistant/components/lorawan/_vendor/lorawan_connection`: common event
  Protocols, fixture dataclasses, and the reusable `DeviceCollection` base.
- `homeassistant/components/lorawan/chirpstack.py`: integration-owned API
  helpers for the generated gRPC client,
  complete inventory polling, individual event streams, and one subscription.
- `homeassistant/components/sensecap/_vendor/sensecap_lorawan`: S2101 decoding,
  model selection, partial state, and state observers.
- `tests/components/lorawan` and `tests/components/sensecap`: isolated tests.

The shared LoRaWAN and SenseCAP libraries are vendored. Their imports use their
temporary Core namespace; they otherwise do not use HA APIs. ChirpStack API
helpers live inside the LoRaWAN integration. Generated ChirpStack bindings,
gRPC, and protobuf remain ordinary dependencies. No JavaScript decoder runs in
HA. The native decoder follows Seeed's seven-byte record layout and the catalog
fixture. It validates length and S2101 ranges, but does not validate the two-byte
trailer: the reference decoder's CRC routine is a stub.

## Library author pattern

Subclass `DeviceCollection` and implement `_create_device(descriptor)`. Return a
model only for reviewed catalog identities. Models expose `descriptor`,
`handle_event(event)`, and `close()`. The collection automatically creates and
retires models; callers never manually add devices.

```python
from homeassistant.components.sensecap._vendor.sensecap_lorawan import (
    SenseCapDeviceCollection,
)

models = SenseCapDeviceCollection(network_id)
stop_added = models.subscribe_device_added(device_added)
stop_removed = models.subscribe_device_removed(device_removed)
stop_events = await connection.async_subscribe(models.handle_event, disconnected)
```

`subscribe_device_added` synchronously reports existing models as well as future
ones. In `device_added`, create entities and subscribe to the model's state.
Forward every event to the collection; decoding, FPorts, state merging, and
unsupported data belong to the vendor library. Close subscriptions and models
when unloading. `test_libraries.py` is a runnable fixture example.

Events are borrowed, read-only Protocols. `DeviceEventData` is a small envelope;
its payload is the original generated protobuf object, not a reconstructed
nested dataclass. Fixture `UplinkData` and `StatusData` implement the same
contracts. Dispatch using `EventType`, not runtime Protocol checks. Only the
adapter imports ChirpStack bindings.

Initial inventory is delivered before `async_subscribe` returns. The adapter
polls every 30 seconds, commits only complete snapshots, and refreshes inventory
before delivering activity for an unknown device. Current per-device streams
usually mean polling discovers a new device first. Unsubscribe stops callback
delivery; the transport owner must await `close()` to release the connection.
Callbacks are synchronous and must not block.

A disconnect makes the provider unavailable before notifying consumers. HA
reloads the collection and uses `ConfigEntryNotReady` until the provider is
available. Rejected credentials trigger the provider's reauthentication flow.
Sleeping sensors keep their last readings; a transport disconnect is different.
Delivery is live-only and missed readings are acceptable. The adapter compares
Redis event IDs with connection time to discard the internal stream's backlog;
HA and ChirpStack clocks must therefore be reasonably aligned.

## Tests

```sh
uv run --no-sync pytest tests/components/lorawan tests/components/sensecap
```

The real-server test is deliberately outside `tests/`, and is never collected
by the normal HA suite. It creates and deletes a dedicated tenant and uses fixed
simulator IDs. **Run it only against the disposable loopback server below.** It
logs into that disposable server's default admin account to create test keys;
the HA integrations themselves use API keys exclusively.

```sh
PYTHONPATH=. uv run --no-sync python -m pytest -p tests.conftest \
  script/lorawan_poc/real_stack.py -s
```

It exercises the real config flow, catalog lookup, read-only tenant key,
OTAA join, encrypted uplink, protobuf event stream, vendor collection, HA sensors,
upgrade to a full tenant key, reload/backlog suppression, a real TCP outage with
automatic retry/recovery, and device removal.
No gateway hardware or RF is involved. It is not a physical SenseCAP test.

## Reproduce the external environment

Use PostgreSQL 16, Redis 7, Mosquitto 2, and ChirpStack **4.19.2**. The verified
amd64 Debian package is:

- URL: https://artifacts.chirpstack.io/packages/4.x/deb/pool/main/c/chirpstack/chirpstack_4.19.2_linux_amd64.deb
- SHA256: `da119728e60f1f258e00ca9238b625656bab720d5ed8b72fb267557235dbdefc`

Extract it with `dpkg-deb -x` rather than installing its system service. Create
an isolated PostgreSQL role/database `lorawan_poc` with password `lorawan_poc`;
install `pg_trgm` in that database. Run Redis on `127.0.0.1:16379`, with persistence
disabled, and Mosquitto on `127.0.0.1:11883`, allowing anonymous local clients.
Use this ChirpStack configuration, alongside its stock `region_eu868.toml` with
MQTT server changed to `tcp://127.0.0.1:11883`:

```toml
[logging]
level = "info"
[postgresql]
dsn = "postgres://lorawan_poc:lorawan_poc@127.0.0.1/lorawan_poc?sslmode=disable"
[redis]
servers = ["redis://127.0.0.1:16379/"]
[network]
net_id = "000000"
enabled_regions = ["eu868"]
[api]
bind = "127.0.0.1:18080"
secret = "local-poc-only-not-a-production-secret"
[integration]
enabled = []
```

Import the `chirpstack/chirpstack-device-profiles` catalog at commit
`ab2db17404bbdb124ae503b521747be2a10cabe1` with
`chirpstack -c CONFIG_DIR import-device-profiles -d CATALOG_DIR`.
Store the output of `chirpstack -c CONFIG_DIR create-api-key --name ha-poc` in
`$LORAWAN_POC_DIR/api-key.txt`, mode 0600. The default test directory is
`/tmp/lorawan-server`.

Build the included `simulator.go` using the module from
https://github.com/brocaar/chirpstack-simulator at commit
`172a3a07c2796d0fcdfe5cf985c964397a71e6c4` and place the binary at
`$LORAWAN_POC_DIR/simulator`. It adapts the upstream `single_uplink` example,
using FPort 1, the S2101 fixture, and EU868 coding rate 4/5. See
`SIMULATOR_LICENSE` for its MIT license. The broker exists only between the
simulated gateway and ChirpStack; HA consumes gRPC exclusively.

The test deletes its tenant, gateway, devices, and tenant keys in cleanup. Stop
the isolated server/broker/Redis and remove the disposable database when done.
Never commit API keys, database files, or runtime logs.

## Before an upstream integration submission

This is a POC, not a claimed Bronze-quality contribution. `quality_scale.yaml`
records remaining distribution/documentation work as `todo`. Full Hassfest
currently rejects that incomplete quality tier; all other Hassfest plugins can
be checked with `--skip-plugins quality_scale`. No other validation is skipped.
Publish the libraries, add official integration documentation and brands, review
the vendor-discovery registration mechanism, and test actual SenseCAP hardware
before an upstream contribution.
