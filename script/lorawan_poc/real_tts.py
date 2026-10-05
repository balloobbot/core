"""Opt-in TTS config flow, raw uplink, TCP recovery and device-removal test.

Use a disposable loopback TTS 3.36.2 server on port 18849. Set
LORAWAN_TTS_ADMIN_KEY_FILE to the local spike-admin user's API key file.
"""

import asyncio
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

import grpc
from lorawan_connection.backend._tts_api import Service, message
import pytest

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from script.lorawan_poc.real_stack import NetworkProxy, grpc_runtime  # noqa: F401


@pytest.mark.usefixtures("socket_enabled")
async def test_real_tts(hass: HomeAssistant) -> None:
    """Real TTS APIs feed HA sensors and recover without replacing vendor models."""
    key_text = await asyncio.to_thread(
        Path(os.environ["LORAWAN_TTS_ADMIN_KEY_FILE"]).read_text
    )
    key = re.search(r"NNSXS\.[A-Za-z0-9.]+", key_text)[0]
    metadata = (("authorization", f"Bearer {key}"),)
    channel = grpc.aio.insecure_channel("127.0.0.1:18849")
    applications = Service(channel, "ApplicationRegistry")
    registry = Service(channel, "EndDeviceRegistry")
    application = Service(channel, "AppAs")
    app_id = "ha-poc-" + uuid4().hex[:8]
    app_ids = message("ApplicationIdentifiers", application_id=app_id)
    ids = message(
        "EndDeviceIdentifiers",
        application_ids=app_ids,
        device_id="greenhouse",
        dev_eui=bytes.fromhex(uuid4().hex[:16]),
    )
    proxy = NetworkProxy(target_port=18849)
    await proxy.start()
    provider = vendor = None
    created = False
    device_created = False
    try:
        await applications.Create(
            message(
                "CreateApplicationRequest",
                application={"ids": app_ids},
                collaborator={"user_ids": {"user_id": "spike-admin"}},
            ),
            metadata=metadata,
        )
        created = True
        token = await Service(channel, "ApplicationAccess").CreateAPIKey(
            message(
                "CreateApplicationAPIKeyRequest",
                application_ids=app_ids,
                name="ha-poc",
                rights=[
                    "RIGHT_APPLICATION_DEVICES_READ",
                    "RIGHT_APPLICATION_TRAFFIC_READ",
                ],
            ),
            metadata=metadata,
        )
        await registry.Create(
            message(
                "CreateEndDeviceRequest",
                end_device={
                    "ids": ids,
                    "name": "TTS greenhouse",
                    "version_ids": {
                        "brand_id": "sensecap",
                        "model_id": "sensecaps2101-temp-humid",
                    },
                },
            ),
            metadata=metadata,
        )
        device_created = True
        await Service(channel, "AsEndDeviceRegistry").Set(
            message(
                "SetEndDeviceRequest",
                end_device={
                    "ids": ids,
                    "version_ids": {
                        "brand_id": "sensecap",
                        "model_id": "sensecaps2101-temp-humid",
                    },
                },
                field_mask={"paths": ["ids", "version_ids"]},
            ),
            metadata=metadata,
        )
        result = await hass.config_entries.flow.async_init(
            "the_things_stack",
            context={"source": SOURCE_USER},
            data={
                "endpoint": f"http://127.0.0.1:{proxy.port}",
                "api_key": token.key,
                "application_ids": [app_id],
            },
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY, result.get("errors")
        provider = result["result"]
        await hass.async_block_till_done(wait_background_tasks=True)
        flow = next(
            flow
            for flow in hass.config_entries.flow.async_progress()
            if flow["handler"] == "sensecap"
        )
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
        vendor = result["result"]
        await hass.async_block_till_done(wait_background_tasks=True)
        manager = vendor.runtime_data
        coordinator = manager.coordinators[(provider.entry_id, ids.dev_eui.hex())]
        model = coordinator.data
        uplink = message(
            "ApplicationUp",
            end_device_ids=ids,
            uplink_message={
                "f_port": 1,
                "frm_payload": bytes.fromhex("01011098530000010210A87A0000AF51"),
                "settings": {
                    "frequency": 868100000,
                    "data_rate": {
                        "lora": {
                            "bandwidth": 125000,
                            "spreading_factor": 7,
                            "coding_rate": "4/5",
                        }
                    },
                },
            },
        )
        async with asyncio.timeout(15):
            while model.temperature != 21.4:
                await application.SimulateUplink(uplink, metadata=metadata)
                await asyncio.sleep(0.1)
        assert hass.states.get("sensor.tts_greenhouse_temperature").state == "21.4"
        assert hass.states.get("sensor.tts_greenhouse_humidity").state == "31.4"
        print(
            "PASS: TTS config flow, discovery and real application stream produce HA readings"
        )
        cli = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "homeassistant.components.sensecap._vendor.sensecap_lorawan",
            "--backend",
            "tts",
            "--server",
            "http://127.0.0.1:18849",
            "--application",
            app_id,
            "--list",
            "--json",
            env={**os.environ, "TTS_API_KEY": token.key},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await cli.communicate()
        assert cli.returncode == 0, stderr.decode()
        assert json.loads(stdout.splitlines()[0])["model"] == "S2101"
        print("PASS: --backend tts CLI lists the real TTS device")
        await proxy.close()
        async with asyncio.timeout(15):
            while (
                hass.states.get("sensor.tts_greenhouse_temperature").state
                != "unavailable"
            ):
                await asyncio.sleep(0.1)
        assert vendor.state is ConfigEntryState.LOADED
        await proxy.start()
        async with asyncio.timeout(40):
            while (
                provider.state is not ConfigEntryState.LOADED
                or hass.states.get("sensor.tts_greenhouse_temperature").state != "21.4"
            ):
                await asyncio.sleep(0.1)
        assert vendor.runtime_data is manager
        assert (
            manager.coordinators[(provider.entry_id, ids.dev_eui.hex())] is coordinator
        )
        assert coordinator.data is model
        print(
            "PASS: real TTS TCP outage recovers using the same vendor manager, coordinator and model"
        )
        await registry.Delete(ids, metadata=metadata)
        device_created = False
        async with asyncio.timeout(10):
            while manager.coordinators:
                await asyncio.sleep(0.1)
        assert hass.states.get("sensor.tts_greenhouse_temperature") is None
        print("PASS: TTS lifecycle deletion removes HA devices and entities")
    finally:
        if vendor is not None:
            await hass.config_entries.async_unload(vendor.entry_id)
        if provider is not None:
            await hass.config_entries.async_unload(provider.entry_id)
        try:
            if device_created:
                await registry.Delete(ids, metadata=metadata)
            if created:
                await Service(channel, "AsEndDeviceRegistry").Delete(
                    ids, metadata=metadata
                )
                await applications.Delete(app_ids, metadata=metadata)
        finally:
            await channel.close()
            await proxy.close()
