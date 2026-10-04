"""Opt-in real-server test, deliberately outside the HA test suite.

Run from Core with:
  uv run --no-sync pytest -p tests.conftest script/lorawan_poc/real_stack.py -s
Requires the isolated server and simulator described in README.md.
"""

import asyncio

from chirpstack_api import api, common
import grpc
import pytest

from homeassistant.components.dragino._vendor.dragino_lorawan import LT22222
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from script.lorawan_poc.real_stack import (  # noqa: F401
    SERVER_DIR,
    NetworkProxy,
    grpc_runtime,
)


@pytest.mark.usefixtures("socket_enabled")
async def test_real_dragino_stack(hass: HomeAssistant) -> None:
    """Actual catalog -> join -> HA relay command -> encrypted downlink -> state report."""
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
    simulator = None
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
                global_only=True, device_id=LT22222.catalog_model_id, limit=100
            ),
            metadata=metadata,
        )
        profile_id = None
        for item in profiles.result:
            if item.region != common.EU868:
                continue
            profile = await profile_api.Get(
                api.GetDeviceProfileRequest(id=item.id), metadata=metadata
            )
            if profile.device_profile.supports_class_c:
                profile_id = item.id
                break
        assert profile_id is not None
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
                    dev_eui="0201010101010102",
                    application_id=application_id,
                    device_profile_id=profile_id,
                    name="Workshop",
                )
            ),
            metadata=metadata,
        )
        await device_api.CreateKeys(
            api.CreateDeviceKeysRequest(
                device_keys=api.DeviceKeys(
                    dev_eui="0201010101010102", nwk_key="03" + "01" * 15
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
            provider.runtime_data.connection.devices[
                "0201010101010102"
            ].catalog_model_id
            == LT22222.catalog_model_id
        )
        flow = next(
            flow
            for flow in hass.config_entries.flow.async_progress()
            if flow["handler"] == "dragino"
        )
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
        vendor = result["result"]
        await hass.async_block_till_done()
        assert hass.states.get("switch.workshop_relay_1").state == "unknown"
        # Let the initial stream attach before sending the real radio frames.
        await asyncio.sleep(1)
        with await asyncio.to_thread(
            (SERVER_DIR / "simulator_dragino.log").open, "w"
        ) as output:
            simulator = await asyncio.create_subprocess_exec(
                str(SERVER_DIR / "simulator_dragino"), stdout=output, stderr=output
            )
            async with asyncio.timeout(35):
                while hass.states.get("switch.workshop_relay_1").state != "off":
                    await asyncio.sleep(0.2)
        assert hass.states.get("switch.workshop_relay_2").state == "off"
        assert hass.states.get("sensor.workshop_voltage_1").state == "1.195"
        assert hass.states.get("sensor.workshop_voltage_2").state == "1.196"
        assert hass.states.get("sensor.workshop_current_1").state == "4.88"
        assert hass.states.get("sensor.workshop_current_2").state == "4.864"
        assert hass.states.get("binary_sensor.workshop_digital_input_1").state == "on"
        assert hass.states.get("binary_sensor.workshop_digital_input_2").state == "off"
        with pytest.raises(HomeAssistantError, match="write permission"):
            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": "switch.workshop_relay_1"},
                blocking=True,
            )
        assert hass.states.get("switch.workshop_relay_1").state == "off"
        print(
            "PASS: real read-only key discovers Dragino, reads both relays, and rejects control"
        )
        hass.config_entries.async_update_entry(
            provider, data={**provider.data, "api_key": writable.token}
        )
        assert await hass.config_entries.async_reload(provider.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_reload(vendor.entry_id)
        await hass.async_block_till_done()
        for entity, action, expected in [
            ("switch.workshop_relay_1", "turn_on", "on"),
            ("switch.workshop_relay_2", "turn_on", "on"),
            ("switch.workshop_relay_1", "turn_off", "off"),
            ("switch.workshop_relay_2", "turn_off", "off"),
            ("switch.workshop_digital_output_1", "turn_on", "on"),
            ("switch.workshop_digital_output_2", "turn_on", "on"),
            ("switch.workshop_digital_output_1", "turn_off", "off"),
            ("switch.workshop_digital_output_2", "turn_off", "off"),
        ]:
            await hass.services.async_call(
                "switch", action, {"entity_id": entity}, blocking=True
            )
            async with asyncio.timeout(30):
                while hass.states.get(entity).state != expected:
                    await asyncio.sleep(0.2)
        log = await asyncio.to_thread((SERVER_DIR / "simulator_dragino.log").read_text)
        for payload in (
            "030111",
            "031101",
            "030011",
            "031100",
            "02011111",
            "02110111",
            "02001111",
            "02110011",
        ):
            assert payload in log, f"Simulator did not receive {payload}"
        print(
            "PASS: HA reads all analog and digital inputs; all outputs await device ACKs and receive state reports"
        )
        await proxy.close()
        async with asyncio.timeout(10):
            while provider.state is ConfigEntryState.LOADED:
                await asyncio.sleep(0.1)
        async with asyncio.timeout(10):
            while vendor.state is not ConfigEntryState.SETUP_RETRY:
                await asyncio.sleep(0.1)
        assert hass.states.get("switch.workshop_relay_1").state == "unavailable"
        await proxy.start()
        async with asyncio.timeout(35):
            while (
                provider.state is not ConfigEntryState.LOADED
                or vendor.state is not ConfigEntryState.LOADED
            ):
                await asyncio.sleep(0.2)
        print(
            "PASS: real TCP outage made switches unavailable; HA retried and recovered automatically"
        )
        await device_api.Delete(
            api.DeleteDeviceRequest(dev_eui="0201010101010102"), metadata=metadata
        )
        await provider.runtime_data.connection.refresh()
        await hass.async_block_till_done()
        assert not vendor.runtime_data.collection.devices
        print("PASS: server device deletion removed the vendor model")
    finally:
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
