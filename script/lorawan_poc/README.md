# LoRaWAN proof of concept

This POC uses published `lorawan-connection==0.10.0`, including the optional
ChirpStack and The Things Stack backends. The integration manifests pin the
released package; no editable library checkout is required.

Use a fresh HA test configuration for this branch. It replaces the old LoRaWAN
server entry with ChirpStack entries and uses each server entry ID as the device
identity namespace. Migration from the earlier experimental branch is not included.

This worktree adds `chirpstack`, `the_things_stack`, `lorawan`, `sensecap`, and `dragino` integrations. It connects to an
existing ChirpStack 4.19 or TTS 3.36 server and discovers provisioned devices. The first
SenseCAP model is S2101, with temperature and humidity sensors. Provisioning,
BLE, gateway setup, a managed HA app, and a custom frontend are later phases.

## Get the POC branch

```sh
git clone --branch lorawan-provider-architecture --single-branch https://github.com/balloobbot/core.git core-lorawan
cd core-lorawan
```

The [implementation findings](https://gisthost.github.io/?c66c965ba7be32f9b0b64ff5c0ce0241#findings)
and [run instructions](https://gisthost.github.io/?c66c965ba7be32f9b0b64ff5c0ce0241#running)
are also available on the proposal website.

## Try it in Home Assistant

1. Run `script/setup` in this worktree. For the tests and CLI examples, install
   the published library with both backend extras:

   ```sh
   uv pip install --python .venv/bin/python 'lorawan-connection[chirpstack,tts]==0.10.0'
   ```

   During normal setup, each server integration installs its required extra.
   Start HA with a fresh configuration:

   ```sh
   uv run --no-sync python -m homeassistant --config ../lorawan-test-config
   ```
2. On ChirpStack, import the current device-profile catalog. Assign the **global
   SenseCAP S2101 catalog profile** for the device's radio region. A custom
   tenant profile with the same name is not enough: ChirpStack ignores the
   `device_id` field when creating a custom profile.
3. Enable ChirpStack's per-device event log (enabled by default). The current
   adapter requires the internal `StreamDeviceEvents` gRPC endpoint.
4. Add integration → ChirpStack. Enter `https://host:port` and an API key. For an
   unencrypted local server, explicitly use `http://host:port`. TLS uses the
   platform's trusted roots; there is no automatic downgrade or insecure TLS.
5. Select a tenant and at least one application. Setup validates device/profile read access before creating the entry. Global keys can list tenants. Tenant-scoped
   keys require their tenant UUID. ChirpStack reports insufficient listing scope
   as UNAUTHENTICATED, so the flow validates the key after selecting a tenant. Full-access and read-only keys both work.
6. Confirm the discovered SenseCAP or Dragino integration. Each vendor has one
   entry covering supported devices across all registered servers. There is no
   connection picker. Application selection in the server integration defines scope.
   Vendor setup can run before servers connect; new registrations are picked up automatically.

The admin-only `lorawan/devices/list` websocket command takes `entry_id` and returns
current descriptors and availability. Each descriptor includes `unsupported_reason`:

- `no_catalog_identity`: the profile has no catalog model or vendor identity. Assign
  the device's imported global catalog profile in ChirpStack; a matching profile
  name alone does not identify the model.
- `no_vendor_integration`: the catalog vendor has no registered HA integration.
- `model_not_supported`: the vendor integration does not yet support this model.
- `null`: the model is supported, even before its vendor integration is configured.

Vendor integrations declare their IDs in `manifest.json` and expose their library’s
model classes in a `lorawan.py` platform. Support reporting uses those declarations.

Missing catalog identity takes precedence. The endpoint exposes no credentials. The regular HA
integration/device/entity pages show the provider, collection, and sensors.

Catalog entries do not require an encoder or decoder. The catalog's device
identity and radio profiles are enough for mapping; our Python libraries decode
uplinks and encode commands. The [catalog contribution guide](https://github.com/chirpstack/chirpstack-device-profiles#add-devices)
makes codecs optional, and the [ChirpStack importer](https://github.com/chirpstack/chirpstack/blob/v4.19.2/chirpstack/src/cmd/import_device_profiles.rs)
retains the device identity without a codec.

## Source layout

- `lorawan-connection`: common event Protocols, fixture
  dataclasses, and the reusable `DeviceCollection` base.
- `lorawan_connection.backend.chirpstack`: shared API helpers for the generated gRPC client,
  complete inventory polling, individual event streams, and one subscription.
- `lorawan_connection.backend.tts`: optional TTS registry, application traffic,
  lifecycle events, and downlink queueing through gRPC.
- `homeassistant/components/sensecap/_vendor/sensecap_lorawan`: S2101 decoding,
  model selection, partial state, and state observers.
- `homeassistant/components/dragino/_vendor/dragino_lorawan`: LT-22222-L models
  and command encoding.
- `tests/components/chirpstack`, `tests/components/the_things_stack`,
  `tests/components/lorawan`, `tests/components/sensecap`, and
  `tests/components/dragino`: isolated tests.

The shared LoRaWAN library is published as version 0.10.0. The SenseCAP and Dragino libraries remain
vendored under temporary Core namespaces; they use no HA APIs and import
`lorawan_connection` directly. ChirpStack API helpers ship in the optional shared-library backend. Generated ChirpStack bindings,
gRPC, and protobuf remain ordinary dependencies. No JavaScript decoder runs in
HA. The native decoder follows Seeed's seven-byte record layout and the catalog
fixture. It validates length and S2101 ranges, but does not validate the two-byte
trailer: the reference decoder's CRC routine is a stub.

## Library author pattern

Subclass `Device` for each model and declare `identifiers`, a mapping from stack
to `(brand_id, model_id)`. Declare supported classes in `DeviceCollection.DEVICES`; the
collection builds its lookup and creates or retires models automatically. Models
update their own attributes and call `notify()`. Consumers use `add_update_listener()`
with callbacks that read those attributes. Override `_create_device()` only for special
matching rules.

```python
manager = entry.runtime_data = lorawan.DeviceManager(
    hass,
    entry,
    create_collection=SenseCapDeviceCollection,
    create_coordinator=SenseCapCoordinator,
)
entry.async_on_unload(manager.close)
await manager.async_setup()
```

The HA device manager creates one ordinary `DataUpdateCoordinator` per device and
owns its lifetime. Platforms use `manager.subscribe_coordinator_added(callback)`
to receive ready coordinators for existing and new devices. All platforms share
the same coordinator for a device. Its `device_info` uses
`lorawan.device_identifier(DOMAIN, device)`; entities return that information.
The manager uses the same identity for registry cleanup without inspecting metadata.

The manager removes registry records on live device removal and reconciles removals
that happened while HA was offline after successful setup. It updates registered
device names when descriptors change. Ordinary unload and failed setup preserve
registry records. Entities inherit `lorawan.LoRaWANEntity`, which handles model
updates and late entity additions. Closing the manager closes its collections and
retires coordinators, leaving the shared connection open.

Call `manager.async_setup()` without a server selection. The manager creates
collections for current connections and attaches connections registered later.
Decoding, FPorts, state merging, and unsupported data stay in the vendor library.
`test_libraries.py` is a runnable fixture example; `test_device_manager.py` exercises
the shared lifecycle across connections.

Events are borrowed, read-only Protocols. `DeviceEventData` is a small envelope;
its payload is the original generated protobuf object, not a reconstructed
nested dataclass. Fixture `UplinkData` and `StatusData` implement the same
contracts. Dispatch using `EventType`, not runtime Protocol checks. Only the
backend and provider config flow import ChirpStack bindings.

Existing devices are delivered before `devices.async_setup()` returns. The adapter
polls every 30 seconds, commits only complete snapshots, and refreshes inventory
before delivering activity for an unknown device. Current per-device streams
usually mean polling discovers a new device first. Unsubscribe stops callback
delivery; the transport owner must await `close()` to release the connection.
Callbacks are synchronous and must not block.

A disconnect withdraws the server registration and makes its coordinators
unavailable. The server integration retries its transport setup or starts a
reauthentication flow. Vendor entries stay loaded. Reconnection reuses existing
models and reconciles the server’s complete device list.

## Try the device-library CLI

The vendored SenseCAP library passes `SenseCapDeviceCollection.DEVICES` to the shared CLI helper:

```sh
uv run --no-sync python -m homeassistant.components.sensecap._vendor.sensecap_lorawan \
  --backend chirpstack --server https://host:port --api-key-file /path/to/key --tenant TENANT_UUID
```

The command watches decoded state. Add `--list` to list devices and exit, or
`--json` for newline-delimited JSON.
The helper selects models, builds the collection, and observes state automatically.
`--backend` selects the adapter. ChirpStack is the default; `tts` selects The Things Stack. The helper imports it only when connecting; help needs no backend extra.
Network operations are async, and key-file reads run in a worker thread.
Event handling, model notifications, and CLI printing remain synchronous.
A published vendor library can expose the same command under its package name.

## Tests

```sh
uv run --no-sync pytest tests/components/chirpstack tests/components/the_things_stack tests/components/lorawan tests/components/sensecap tests/components/dragino tests/hassfest/test_lorawan.py
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
upgrade to a full tenant key, reload with stable identity, a real TCP outage with
automatic retry/recovery using the same model and coordinator, and device removal.
It also verifies the CLI’s inventory and decoded state against
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
currently rejects that incomplete quality tier. The manifests pin the published
library, including both optional backends.
Publish the SenseCAP and Dragino libraries, add official integration documentation and brands, review
the vendor-discovery registration mechanism, and test actual SenseCAP hardware
before an upstream contribution.

## Dragino example

The Dragino LT-22222-L is available from Seeed. It uses the separate Dragino
integration. It already appears in the
upstream ChirpStack catalog; no local catalog entry is needed.

1. Provision an LT-22222-L in ChirpStack using its global **Class C** profile for
   the device's region. Configure the hardware for Class C and working mode 1–5.
2. Select its application in the ChirpStack integration. Confirm the discovered Dragino
   integration once. Its manager creates one collection per registered server.
3. The integration creates switches, binary sensors, and sensors for the model.
   Values start unknown until an uplink reports them.
4. Turn an output on or off. The library encodes FPort 2 commands and passes them through
   the LoRaWAN provider to ChirpStack's public enqueue API. It requests a confirmed
   downlink and waits up to 30 seconds for the device ACK. The other outputs are unchanged.
5. HA updates the switch after a device report. Queue acceptance and protocol
   acknowledgements do not imply the physical relay changed.

Read-only keys can discover and monitor relays. An attempted write reports a clear
error; replace the provider key with one that has tenant write access to enable control.
Commands have no queue expiry by default. Library callers can pass `expires_at`
on backends that support it. HA stops waiting after 30 seconds, but the command
can still be delivered later. There are no automatic command retries or queue flushes.

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
sends a new acknowledged relay command through the recovered connection using the
same model. It removes models when the server device is deleted. Run it sequentially
with the SenseCAP radio test because both use the same simulated gateway ID.

The generic CLI can also observe the Dragino models:

```sh
uv run --no-sync python -m homeassistant.components.dragino._vendor.dragino_lorawan \
  --backend chirpstack --server https://host:port --api-key-file /path/to/key --tenant TENANT_UUID --json
```

Protocol: https://wiki.dragino.com/docs/LoRaWAN-End-Node/io-controllers-sensor-nodes/lt-22222-l/
Catalog: https://github.com/chirpstack/chirpstack-device-profiles/blob/master/vendors/dragino/devices/lt-22222-l.toml

## Device removal

Collection callbacks remove devices through the HA device registry. This also removes
active entities and entity registry records, including disabled entities. After a
successful collection setup, the integration removes registry devices absent from
the current collection. This catches removals while HA was offline. Failed setup
and ordinary unload preserve registry records.

Compare vendored examples with the library checkout:

```sh
uv run --no-sync python script/lorawan_poc/check_vendor_examples.py ../lorawan-connection
```


## Stack identities

Discovery registrations are stack and brand pairs, for example
`"lorawan": [["chirpstack", 744], ["tts", "sensecap"]]`. Both ChirpStack and The Things Stack register connections. The TTS external
harness remains in the library’s `script/tts_spike/` directory and exercises the
packaged backend.

## Connection registration

ChirpStack owns credentials, transport recovery, and reauthentication. It passes
its config entry ID as the backend's `network_id`, then calls
`await lorawan.async_register_connection(hass, entry, connection=connection)`.
The returned callback withdraws the registration without closing the transport.

The LoRaWAN device manager keeps one collection per server. Temporary disconnection
makes only that server's coordinators unavailable. Reconnection keeps existing
models and entities, updates descriptors, and removes devices missing from the
new device list. Pending command waits fail on disconnect.

Device deletion, loss of model support, or deletion of a server config entry
removes device and entity registry records. Startup checks for deleted server
entries and reconciles each server only when its complete device list is available.
An offline server's devices remain registered.

## The Things Stack

Add integration → The Things Stack. Enter the Application Server gRPC URL, an
application API key, and the application IDs to expose. Set the optional Identity
Server URL when it differs from the Application Server. Use `https://host:8884`
for TLS or explicit `http://host:port` for a disposable local server.

The key needs device-read and traffic-read rights for every selected application.
Traffic-down-write enables commands. The integration validates access, registers
its backend with LoRaWAN, and owns recovery and reauthentication. The existing
SenseCAP and Dragino entries attach automatically; no connection picker is needed.

The TTS backend reads catalog identity from each device's `version_ids`. An S2101
uses `sensecap` / `sensecaps2101-temp-humid`. Devices need a DevEUI. The backend
uses gRPC application and lifecycle streams plus a 30-second inventory refresh.

TTS has no downlink queue expiry. Commands with `expires_at` fail before enqueueing.
The Dragino relay and digital-output methods default to no expiry, so they can
queue commands and await acknowledgements through TTS. Queue clearing is a
separate server operation; the integration does not clear queues on timeout.
No physical TTS radio hardware or hosted Community Edition deployment was tested.

To run the real TTS setup, CLI, uplink, TCP outage/recovery, and removal test,
start the disposable TTS 3.36.2 environment described in the library's
`script/tts_spike/README.md`. Then run:

```sh
LORAWAN_TTS_ADMIN_KEY_FILE=/path/to/local-test-admin-key.txt PYTHONPATH=. \
  uv run --no-sync python -m pytest -p tests.conftest script/lorawan_poc/real_tts.py -s
```

The administrator key prepares and cleans up an isolated test application.
The HA integration and CLI receive a read-only application key. The test uses
loopback port 18849 and simulates application traffic, not RF reception.

For the device-library CLI:

```sh
uv run --no-sync python -m homeassistant.components.sensecap._vendor.sensecap_lorawan \
  --backend tts --server https://application-server:8884 \
  --identity-server https://identity-server:8884 --application my-app \
  --api-key-file /path/to/application-key --json
```

ChirpStack and TTS may be configured together. A server outage affects only its
own devices; reconnection preserves the vendor manager and existing models.
