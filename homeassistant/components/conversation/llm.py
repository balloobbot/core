"""LLM tools for the conversation integration."""

import json
from typing import override

from hassil.models import MatchEntity
from hassil.parse_expression import parse_sentence
import probatio

from homeassistant.components.llm import LLMTools
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.llm import (
    LLM_API_ASSIST,
    LLMContext,
    Tool,
    ToolInput,
    ToolResult,
)

from .agent_manager import get_agent_manager
from .chat_log import current_chat_log
from .const import DOMAIN
from .default_agent import _collect_list_references
from .models import ConversationInput


class TriggerSentenceTool(Tool):
    """LLM tool to run the automations of a sentence trigger."""

    name = f"{DOMAIN}__trigger_sentence"
    title = "Run voice command"
    description = (
        "Run a voice command that the user defined in an automation. "
        "The prompt lists the sentences."
    )
    integration = DOMAIN

    def __init__(self, sentences: list[str]) -> None:
        """Init the tool."""
        self.parameters = probatio.Schema(
            {
                probatio.Required(
                    "sentence",
                    description="The sentence of the voice command, as listed.",
                ): probatio.In(sentences),
                probatio.Optional(
                    "slots",
                    description=(
                        "Values for the {name} parts of the sentence, keyed by name."
                    ),
                    default={},
                ): {str: str},
            }
        )

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> ToolResult:
        """Run the triggers of the sentence."""
        data = self.parameters(tool_input.tool_args)
        sentence: str = data["sentence"]
        slots: dict[str, str] = data["slots"]

        slot_names: set[str] = set()
        _collect_list_references(parse_sentence(sentence).expression, slot_names)
        if unknown := set(slots) - slot_names:
            return ToolResult(
                data={
                    "error": f"Unknown slots {sorted(unknown)}. "
                    f"This sentence has the slots {sorted(slot_names)}."
                },
                error=True,
            )

        # Automations read the user's request from trigger.sentence.
        text = sentence
        conversation_id = None
        if (chat_log := current_chat_log.get()) is not None:
            conversation_id = chat_log.conversation_id
            for content in reversed(chat_log.content):
                if content.role == "user":
                    text = content.content
                    break

        user_input = ConversationInput(
            text=text,
            context=llm_context.context or Context(),
            conversation_id=conversation_id,
            device_id=llm_context.device_id,
            satellite_id=llm_context.satellite_id,
            language=llm_context.language or hass.config.language,
            agent_id=llm_context.agent_id or DOMAIN,
        )
        entities = {
            name: MatchEntity(name=name, value=value, text=value, is_wildcard=True)
            for name, value in slots.items()
        }

        manager = get_agent_manager(hass)
        assert manager.default_agent is not None
        # Look up the triggers now, as automations may reload during the
        # conversation.
        response = await manager.default_agent.async_run_trigger_callbacks(
            [
                trigger.callback(user_input, entities)
                for trigger in manager.triggers
                if sentence in trigger.sentences
            ],
            user_input.language,
        )
        return ToolResult(data={"response": response})


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    """Return the sentence trigger tool and the sentences to match."""
    if api_id != LLM_API_ASSIST:
        return None

    triggers = get_agent_manager(hass).triggers
    if not triggers:
        return None

    # The tool description is not enough. A model that searches for deferred
    # tools by keyword would miss a request that only matches in meaning.
    lines = [
        "The user defined voice commands in automations. When a request matches "
        "one of these sentences in meaning, even if the wording differs, call "
        f"{TriggerSentenceTool.name} with that sentence. In a sentence, [x] is "
        "optional, (a|b) is a choice, and {name} is a value to pass in slots."
    ]
    entries = []
    for trigger in triggers:
        sentences = ", ".join(
            json.dumps(sentence, ensure_ascii=False) for sentence in trigger.sentences
        )
        entries.append(
            f"- {trigger.name}: {sentences}" if trigger.name else f"- {sentences}"
        )
    # Editing an automation in the UI registers a second copy of its trigger.
    lines.extend(dict.fromkeys(entries))

    all_sentences = list(
        dict.fromkeys(
            sentence for trigger in triggers for sentence in trigger.sentences
        )
    )
    return LLMTools(tools=[TriggerSentenceTool(all_sentences)], prompt="\n".join(lines))
