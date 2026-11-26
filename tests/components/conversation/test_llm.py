"""Tests for the conversation LLM tools platform."""

import probatio
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components import conversation, llm as llm_component
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import (
    chat_session,
    device_registry as dr,
    entity_registry as er,
    llm,
)
from homeassistant.setup import async_setup_component

TOOL_NAME = "conversation__trigger_sentence"


@pytest.fixture
async def setup_triggers(hass: HomeAssistant, init_components) -> None:
    """Set up the LLM integration and automations with sentence triggers."""
    assert await async_setup_component(hass, "llm", {})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "Party mode",
                    "trigger": {
                        "platform": "conversation",
                        "command": [
                            "start party mode in [the] {room}",
                            "party time",
                        ],
                    },
                    "action": {
                        "set_conversation_response": (
                            "Party in {{ trigger.slots.room }}, "
                            "{{ trigger.sentence }}, {{ trigger.satellite_id }}"
                        ),
                    },
                },
                {
                    "alias": "Horses",
                    "trigger": {
                        "platform": "conversation",
                        "command": ["talk about horses", "party time"],
                    },
                    "action": [],
                },
            ]
        },
    )


@pytest.fixture
def satellite_device_id(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> str:
    """Register a satellite device and return its ID."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "kitchen")}
    )
    entity_registry.async_get_or_create(
        "sensor", "test", "kitchen_sensor", device_id=device.id
    )
    entity_registry.async_get_or_create(
        "assist_satellite",
        "test",
        "kitchen",
        device_id=device.id,
        suggested_object_id="kitchen",
    )
    return device.id


def _llm_context(device_id: str | None = None) -> llm.LLMContext:
    """Return an LLM context for a conversation agent."""
    return llm.LLMContext(
        platform="test",
        context=Context(),
        language="en",
        assistant=conversation.DOMAIN,
        device_id=device_id,
        agent_id="conversation.test",
    )


@pytest.mark.usefixtures("init_components")
async def test_no_triggers(hass: HomeAssistant) -> None:
    """Test there is no tool without sentence triggers."""
    assert await async_setup_component(hass, "llm", {})
    result = await llm_component.async_get_tools(hass, _llm_context(), "assist")
    assert TOOL_NAME not in {tool.name for tool in result.tools}


@pytest.mark.usefixtures("setup_triggers")
async def test_only_assist_api(hass: HomeAssistant) -> None:
    """Test the tool is only part of the Assist API."""
    result = await llm_component.async_get_tools(
        hass, _llm_context(), llm.LLM_API_HOME_ASSISTANT
    )
    assert TOOL_NAME not in {tool.name for tool in result.tools}


@pytest.mark.usefixtures("setup_triggers")
async def test_prompt_and_parameters(hass: HomeAssistant) -> None:
    """Test the prompt lists the sentences per automation."""
    result = await llm_component.async_get_tools(hass, _llm_context(), "assist")
    tool = next(tool for tool in result.tools if tool.name == TOOL_NAME)

    assert result.prompt is not None
    assert (
        '- Party mode: "start party mode in [the] {room}", "party time"\n'
        '- Horses: "talk about horses", "party time"'
    ) in result.prompt

    # A sentence shared by two automations is one choice.
    assert probatio.to_openapi(tool.parameters)["properties"]["sentence"]["enum"] == [
        "start party mode in [the] {room}",
        "party time",
        "talk about horses",
    ]


@pytest.mark.usefixtures("setup_triggers")
async def test_call_with_slots(hass: HomeAssistant, satellite_device_id: str) -> None:
    """Test the tool runs the automation with the slots, user text and satellite."""
    api = await llm.async_get_api(hass, "assist", _llm_context(satellite_device_id))
    user_input = conversation.ConversationInput(
        text="let's get the kitchen going",
        context=Context(),
        conversation_id=None,
        device_id=None,
        satellite_id="assist_satellite.kitchen",
        language="en",
        agent_id="conversation.test",
    )
    with (
        chat_session.async_get_chat_session(hass, context=None) as session,
        conversation.async_get_chat_log(hass, session, user_input),
    ):
        result = await api.async_call_tool(
            llm.ToolInput(
                tool_name=TOOL_NAME,
                tool_args={
                    "sentence": "start party mode in [the] {room}",
                    "slots": {"room": "kitchen"},
                },
            )
        )

    assert result == llm.ToolResult(
        data={
            "response": (
                "Party in kitchen, let's get the kitchen going, "
                "assist_satellite.kitchen"
            )
        }
    )


@pytest.mark.usefixtures("setup_triggers")
async def test_call_shared_sentence(hass: HomeAssistant) -> None:
    """Test a sentence shared by two automations runs both."""
    calls: list[str] = []
    hass.bus.async_listen(
        "automation_triggered", lambda event: calls.append(event.data["name"])
    )
    api = await llm.async_get_api(hass, "assist", _llm_context())
    result = await api.async_call_tool(
        llm.ToolInput(tool_name=TOOL_NAME, tool_args={"sentence": "party time"})
    )
    await hass.async_block_till_done()

    assert sorted(calls) == ["Horses", "Party mode"]
    # Without a chat log, the sentence stands in for the user's text.
    assert result.data["response"] == "Party in , party time, None"


@pytest.mark.usefixtures("setup_triggers")
async def test_call_default_response(hass: HomeAssistant) -> None:
    """Test an automation without a response returns the default response."""
    api = await llm.async_get_api(hass, "assist", _llm_context())
    result = await api.async_call_tool(
        llm.ToolInput(tool_name=TOOL_NAME, tool_args={"sentence": "talk about horses"})
    )
    assert result == llm.ToolResult(data={"response": "Done"})


@pytest.mark.usefixtures("setup_triggers")
async def test_call_unknown_slot(hass: HomeAssistant) -> None:
    """Test a slot that is not in the sentence returns an error."""
    api = await llm.async_get_api(hass, "assist", _llm_context())
    result = await api.async_call_tool(
        llm.ToolInput(
            tool_name=TOOL_NAME,
            tool_args={
                "sentence": "start party mode in [the] {room}",
                "slots": {"area": "kitchen"},
            },
        )
    )
    assert result == llm.ToolResult(
        data={"error": "Unknown slots ['area']. This sentence has the slots ['room']."},
        error=True,
    )


@pytest.mark.usefixtures("setup_triggers")
async def test_call_unknown_sentence(hass: HomeAssistant) -> None:
    """Test a sentence that is not listed is rejected."""
    api = await llm.async_get_api(hass, "assist", _llm_context())
    with pytest.raises(probatio.Invalid):
        await api.async_call_tool(
            llm.ToolInput(tool_name=TOOL_NAME, tool_args={"sentence": "party"})
        )
