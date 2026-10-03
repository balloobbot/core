# LoRaWAN proof of concept

The POC uses `lorawan-connection[chirpstack]==0.7.0` from PyPI, including shared
connections, device collections, and confirmed commands that wait for device ACKs.

This worktree adds `lorawan`, `sensecap`, and `dragino` integrations. It connects to an
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

1. Run `script/setup` in this worktree. For direct test runs, install the
   integration dependencies with
   `uv pip install "lorawan-connection[chirpstack]==0.7.0"`.
   HA installs the package and its backend dependencies from the LoRaWAN manifest when setting up the integration.
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

- Published `lorawan-connection[chirpstack]==0.7.0`: common event Protocols, fixture
  dataclasses, and the reusable `DeviceCollection` base.
- `lorawan_connection.chirpstack`: shared API helpers for the generated gRPC client,
  complete inventory polling, individual event streams, and one subscription.
- `homeassistant/components/sensecap/_vendor/sensecap_lorawan`: S2101 decoding,
  model selection, partial state, and state observers.
- `tests/components/lorawan` and `tests/components/sensecap`: isolated tests.

The shared LoRaWAN library comes from PyPI. The SenseCAP and Dragino libraries remain
vendored under its temporary Core namespace; it uses no HA APIs and imports
`lorawan_connection` directly. ChirpStack API helpers ship in the optional shared-library backend. Generated ChirpStack bindings,
gRPC, and protobuf remain ordinary dependencies. No JavaScript decoder runs in
HA. The native decoder follows Seeed's seven-byte record layout and the catalog
fixture. It validates length and S2101 ranges, but does not validate the two-byte
trailer: the reference decoder's CRC routine is a stub.

## Library author pattern

Subclass `Device` for each model and declare its `vendor_id` and
`catalog_model_id`. Declare supported classes in `DeviceCollection.DEVICES`; the
collection builds its lookup and creates or retires models automatically. Models
update their own attributes and call `notify()`. Consumers use `add_update_listener()`
with callbacks that read those attributes. Override `_create_device()` only for special
matching rules.

```python
from homeassistant.components.sensecap._vendor.sensecap_lorawan import (
    SenseCapDeviceCollection,
)

models = SenseCapDeviceCollection(connection)
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
backend and provider config flow import ChirpStack bindings.

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

## Try the device-library CLI

The vendored SenseCAP library passes `SenseCapDeviceCollection.DEVICES` to the shared CLI helper:

```sh
uv run --no-sync python -m homeassistant.components.sensecap._vendor.sensecap_lorawan \
  --server https://host:port --api-key-file /path/to/key --tenant TENANT_UUID --list
```

Remove `--list` to watch decoded state. Add `--json` for newline-delimited JSON.
The helper selects models, builds the collection, and observes state automatically.
Network operations are async, and key-file reads run in a worker thread.
Event handling, model notifications, and CLI printing remain synchronous.
The standalone Python library will expose the same command under its package name.

## Tests

```sh
uv run --no-sync pytest tests/components/lorawan tests/components/sensecap tests/components/dragino
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
automatic retry/recovery, and device removal. It also verifies the CLI’s inventory and decoded state against
the same real uplink.
No gateway hardware or RF is involved. Physical SenseCAP and Dragino devices have not been tested.

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

Build both simulators using a checkout of
https://github.com/brocaar/chirpstack-simulator at commit
`172a3a07c2796d0fcdfe5cf985c964397a71e6c4`:

```sh
uv run --no-sync python script/lorawan_poc/build_simulators.py /path/to/chirpstack-simulator /tmp/lorawan-server
```

The builder uses a temporary Go source overlay to add a payload callback and
confirmed-downlink ACKs to the upstream simulator. The checkout remains unchanged. The Dragino simulator receives
encrypted downlinks, changes its emulated outputs, and includes input readings in
subsequent encrypted uplinks. See `SIMULATOR_LICENSE` for the upstream MIT license.
The broker exists between the simulated gateway and ChirpStack; HA uses gRPC.

The test deletes its tenant, gateway, devices, and tenant keys in cleanup. Stop
the isolated server/broker/Redis and remove the disposable database when done.
Never commit API keys, database files, or runtime logs.

## Before an upstream integration submission

This is a POC, not a claimed Bronze-quality contribution. `quality_scale.yaml`
records remaining distribution/documentation work as `todo`. Full Hassfest
currently rejects that incomplete quality tier; all other Hassfest plugins can
be checked with `--skip-plugins quality_scale`. No other validation is skipped.
Publish the SenseCAP library, add official integration documentation and brands, review
the vendor-discovery registration mechanism, and test actual SenseCAP hardware
before an upstream contribution.

## Dragino example

The Dragino LT-22222-L is available from Seeed. It uses the separate Dragino
integration. It already appears in the
upstream ChirpStack catalog; no local catalog entry is needed.

1. Provision an LT-22222-L in ChirpStack using its global **Class C** profile for
   the device's region. Configure the hardware for Class C and working mode 1–5.
2. Select its application in the LoRaWAN integration. Confirm the discovered Dragino
   integration. One collection contains all supported Dragino devices on that network.
3. The integration creates switches, binary sensors, and sensors for the model.
   Values start unknown until an uplink reports them.
4. Turn an output on or off. The library encodes FPort 2 commands and passes them through
   the LoRaWAN provider to ChirpStack's public enqueue API. It requests a confirmed
   downlink and waits up to 30 seconds for the device ACK. The other outputs are unchanged.
5. HA updates the switch after a device report. Queue acceptance and protocol
   acknowledgements do not imply the physical relay changed.

Read-only keys can discover and monitor relays. An attempted write reports a clear
error; replace the provider key with one that has tenant write access to enable control.
Commands expire after 30 seconds in the server queue. There are no automatic command
retries or queue flushes.

Voltage readings use volts and current readings use milliamperes. Digital inputs
report high/low in mode 1; counting modes expose the corresponding counters.
Mode changes clear readings no longer reported. Digital output switches are on
when their active-low output is enabled. Trigger-only reports do not replace input
measurements or output states.

Run the full command cycle against the disposable server:

```sh
PYTHONPATH=. uv run --no-sync python -m pytest -p tests.conftest script/lorawan_poc/real_dragino.py -s
```

This tests all output commands through encrypted downlinks, waits for ACKs, checks
input readings and resulting output states, rejects a read-only key, recovers after a TCP outage, and
removes models when the server device is deleted. Run it separately from the SenseCAP
radio test because both use the same simulated gateway ID.

The generic CLI can also observe the Dragino models:

```sh
uv run --no-sync python -m homeassistant.components.dragino._vendor.dragino_lorawan \
  --server https://host:port --api-key-file /path/to/key --tenant TENANT_UUID --json
```

Protocol: https://wiki.dragino.com/docs/LoRaWAN-End-Node/io-controllers-sensor-nodes/lt-22222-l/
Catalog: https://github.com/chirpstack/chirpstack-device-profiles/blob/master/vendors/dragino/devices/lt-22222-l.toml
