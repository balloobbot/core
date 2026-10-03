"""Opt-in real-server test, deliberately outside the HA test suite.

Run from Core with:
  uv run --no-sync pytest -p tests.conftest script/lorawan_poc/real_stack.py -s
Requires the isolated server and simulator described in README.md.
"""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from chirpstack_api import api, common
import grpc
import pytest

from homeassistant.components.sensecap._vendor.sensecap_lorawan import S2101_MODEL_ID
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

SERVER_DIR = Path(
    os.environ.get(
        "LORAWAN_POC_DIR", str(Path(tempfile.gettempdir()) / "lorawan-server")
    )
)


class NetworkProxy:
    """Cut the real gRPC TCP connection without stopping the test server."""

    def __init__(self) -> None:
        """Track accepted test connections."""
        self.port = 0
        self.tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Listen on the same local port after recovery."""
        self.server = await asyncio.start_server(self.client, "127.0.0.1", self.port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Forward one gRPC connection byte for byte."""
        task = asyncio.current_task()
        self.tasks.add(task)
        remote_writer = None
        try:
            remote_reader, remote_writer = await asyncio.open_connection(
                "127.0.0.1", 18080
            )

            async def pump(
                source: asyncio.StreamReader, destination: asyncio.StreamWriter
            ) -> None:
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()
                destination.close()

            await asyncio.gather(
                pump(reader, remote_writer), pump(remote_reader, writer)
            )
        except ConnectionError:
            pass
        finally:
            writer.close()
            if remote_writer:
                remote_writer.close()
            self.tasks.discard(task)

    async def close(self) -> None:
        """Drop connections before waiting for server shutdown."""
        self.server.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.server.wait_closed()


@pytest.fixture(scope="session", autouse=True)
def grpc_runtime():
    """Own gRPC's process-wide completion queue outside per-test leak checks."""
    grpc.aio.init_grpc_aio()
    yield
    grpc.aio.shutdown_grpc_aio()


@pytest.mark.usefixtures("socket_enabled")
async def test_real_stack(hass: HomeAssistant) -> None:
    """Actual catalog -> join -> uplink -> stream -> HA sensors, then removal."""
    token = next(
        line.removeprefix("token: ")
        for line in (
            await asyncio.to_thread((SERVER_DIR / "api-key.txt").read_text)
        ).splitlines()
        if line.startswith("token: ")
    )
    channel = grpc.aio.insecure_channel("127.0.0.1:18080")
    metadata = (("authorization", f"Bearer {token}"),)
    tenant_api, app_api = (
        api.TenantServiceStub(channel),
        api.ApplicationServiceStub(channel),
    )
    profile_api, device_api = (
        api.DeviceProfileServiceStub(channel),
        api.DeviceServiceStub(channel),
    )
    gateway_api = api.GatewayServiceStub(channel)
    tenant_id = (
        await tenant_api.Create(
            api.CreateTenantRequest(
                tenant=api.Tenant(name="HA POC", can_have_gateways=True)
            ),
            metadata=metadata,
        )
    ).id
    provider = vendor = None
    simulator = cli = None
    proxy = NetworkProxy()
    await proxy.start()
    try:
        application_id = (
            await app_api.Create(
                api.CreateApplicationRequest(
                    application=api.Application(
                        tenant_id=tenant_id, name="HA POC sensors"
                    )
                ),
                metadata=metadata,
            )
        ).id
        profiles = await profile_api.List(
            api.ListDeviceProfilesRequest(
                global_only=True, device_id=S2101_MODEL_ID, limit=100
            ),
            metadata=metadata,
        )
        profile_id = next(
            item.id for item in profiles.result if item.region == common.EU868
        )
        await gateway_api.Create(
            api.CreateGatewayRequest(
                gateway=api.Gateway(
                    gateway_id="0101010101010101",
                    tenant_id=tenant_id,
                    name="Simulated gateway",
                )
            ),
            metadata=metadata,
        )
        await device_api.Create(
            api.CreateDeviceRequest(
                device=api.Device(
                    dev_eui="0201010101010101",
                    application_id=application_id,
                    device_profile_id=profile_id,
                    name="Greenhouse",
                )
            ),
            metadata=metadata,
        )
        await device_api.CreateKeys(
            api.CreateDeviceKeysRequest(
                device_keys=api.DeviceKeys(
                    dev_eui="0201010101010101", nwk_key="03" + "01" * 15
                )
            ),
            metadata=metadata,
        )
        internal = api.InternalServiceStub(channel)
        login = await internal.Login(api.LoginRequest(email="admin", password="admin"))
        admin_metadata = (("authorization", f"Bearer {login.jwt}"),)
        readonly = await internal.CreateApiKey(
            api.CreateApiKeyRequest(
                api_key=api.ApiKey(
                    name="HA read-only test", tenant_id=tenant_id, is_read_only=True
                )
            ),
            metadata=admin_metadata,
        )
        writable = await internal.CreateApiKey(
            api.CreateApiKeyRequest(
                api_key=api.ApiKey(name="HA full test", tenant_id=tenant_id)
            ),
            metadata=admin_metadata,
        )
        cli_command = [
            sys.executable,
            "-m",
            "homeassistant.components.sensecap._vendor.sensecap_lorawan",
            "--server",
            "http://127.0.0.1:18080",
            "--tenant",
            tenant_id,
            "--application",
            application_id,
            "--json",
        ]
        cli_env = {**os.environ, "CHIRPSTACK_API_KEY": readonly.token}
        listing = await asyncio.create_subprocess_exec(
            *cli_command,
            "--list",
            env=cli_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        list_output, list_errors = await listing.communicate()
        assert listing.returncode == 0, list_errors.decode()
        listed = [json.loads(line) for line in list_output.splitlines()]
        assert len(listed) == 1
        assert listed[0]["dev_eui"] == "0201010101010101"
        assert listed[0]["model"] == "S2101"
        cli = await asyncio.create_subprocess_exec(
            *cli_command,
            env=cli_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(15):
            initial = json.loads(await cli.stdout.readline())
        assert initial["type"] == "added"
        setup_flow = await hass.config_entries.flow.async_init(
            "lorawan",
            context={"source": SOURCE_USER},
            data={
                "endpoint": f"http://127.0.0.1:{proxy.port}",
                "api_key": readonly.token,
            },
        )
        assert setup_flow["step_id"] == "tenant", setup_flow.get("errors")
        setup_flow = await hass.config_entries.flow.async_configure(
            setup_flow["flow_id"], {"tenant_id": tenant_id}
        )
        assert setup_flow["step_id"] == "applications"
        setup_flow = await hass.config_entries.flow.async_configure(
            setup_flow["flow_id"], {"application_ids": [application_id]}
        )
        assert setup_flow["type"] == FlowResultType.CREATE_ENTRY
        provider = setup_flow["result"]
        await hass.async_block_till_done()
        assert (
            provider.runtime_data.devices["0201010101010101"].catalog_model_id
            == S2101_MODEL_ID
        )
        flow = next(
            flow
            for flow in hass.config_entries.flow.async_progress()
            if flow["handler"] == "sensecap"
        )
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
        vendor = result["result"]
        await hass.async_block_till_done()
        assert hass.states.get("sensor.greenhouse_temperature").state == "unknown"
        # Let the initial stream attach before sending the real radio frames.
        await asyncio.sleep(1)
        with await asyncio.to_thread(
            (SERVER_DIR / "simulator.log").open, "w"
        ) as output:
            simulator = await asyncio.create_subprocess_exec(
                str(SERVER_DIR / "simulator"), stdout=output, stderr=output
            )
            async with asyncio.timeout(35):
                while hass.states.get("sensor.greenhouse_temperature").state != "21.4":
                    await asyncio.sleep(0.2)
        assert hass.states.get("sensor.greenhouse_humidity").state == "31.4"
        async with asyncio.timeout(10):
            state = json.loads(await cli.stdout.readline())
        assert state["type"] == "state"
        assert state["state"] == {"temperature": 21.4, "humidity": 31.4}
        print(
            "PASS: generic model-list CLI discovers S2101 and prints decoded live state"
        )
        print(
            "PASS: real OTAA join and encrypted uplink became HA temperature=21.4, humidity=31.4"
        )
        print(
            "PASS: tenant read-only key resolves the catalog and receives live device events"
        )
        hass.config_entries.async_update_entry(
            provider, data={**provider.data, "api_key": writable.token}
        )
        # Reload must not revive retained uplinks as new measurements.
        assert await hass.config_entries.async_reload(provider.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_reload(vendor.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(2)
        assert hass.states.get("sensor.greenhouse_temperature").state == "unknown"
        print(
            "PASS: upgraded to full tenant key; reload retained stable device identity and suppressed historical stream backlog"
        )
        await proxy.close()
        async with asyncio.timeout(10):
            while provider.state is ConfigEntryState.LOADED:
                await asyncio.sleep(0.1)
        async with asyncio.timeout(10):
            while vendor.state is not ConfigEntryState.SETUP_RETRY:
                await asyncio.sleep(0.1)
        assert hass.states.get("sensor.greenhouse_temperature").state == "unavailable"
        await proxy.start()
        async with asyncio.timeout(35):
            while (
                provider.state is not ConfigEntryState.LOADED
                or vendor.state is not ConfigEntryState.LOADED
            ):
                await asyncio.sleep(0.2)
        print(
            "PASS: real TCP outage made sensors unavailable; HA retried and recovered automatically"
        )
        await device_api.Delete(
            api.DeleteDeviceRequest(dev_eui="0201010101010101"), metadata=metadata
        )
        await provider.runtime_data.connection.refresh()
        await hass.async_block_till_done()
        assert not vendor.runtime_data.devices
        print("PASS: server device deletion removed the vendor model")
    finally:
        if cli is not None and cli.returncode is None:
            cli.terminate()
            await cli.wait()
        if simulator is not None and simulator.returncode is None:
            simulator.terminate()
            await simulator.wait()
        if vendor is not None:
            await hass.config_entries.async_unload(vendor.entry_id)
        if provider is not None:
            await hass.config_entries.async_unload(provider.entry_id)
        await tenant_api.Delete(
            api.DeleteTenantRequest(id=tenant_id), metadata=metadata
        )
        await channel.close()
        await proxy.close()
