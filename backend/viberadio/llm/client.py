"""Claude access via the Claude Agent SDK.

The SDK drives the locally installed Claude Code CLI, so authentication comes from the
user's existing Claude Code login (subscription) — there is no API key anywhere.

Every call is a one-shot, tool-less query whose answer must be a single JSON object
validated against a Pydantic schema. Calls are serialized: only one CLI subprocess at
a time.
"""

import asyncio
import json
import logging
import re
from typing import TypeVar

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
from pydantic import BaseModel, ValidationError

from ..config import settings

log = logging.getLogger("llm")

T = TypeVar("T", bound=BaseModel)

_lock = asyncio.Lock()

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class LLMError(Exception):
    pass


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = fenced.group(1) if fenced else None
    if blob is None:
        m = _JSON_RE.search(text)
        if m is None:
            raise LLMError(f"No JSON object in response: {text[:200]!r}")
        blob = m.group(0)
    return json.loads(blob)


async def _raw_query(system_prompt: str, prompt: str) -> str:
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=[],
        max_turns=1,
        permission_mode="default",
    )
    chunks: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "\n".join(chunks).strip()


async def ask_json(system_prompt: str, prompt: str, schema: type[T]) -> T:
    """Ask Claude for one JSON object and validate it against `schema`.

    Retries once with the parse error appended; raises LLMError on second failure so
    callers can fall back to their deterministic path.
    """
    schema_json = json.dumps(schema.model_json_schema(), indent=None)
    instruction = (
        f"{prompt}\n\n"
        f"Respond with ONLY a single JSON object matching this JSON Schema "
        f"(no prose, no code fence):\n{schema_json}"
    )
    async with _lock:
        for attempt in (1, 2):
            try:
                text = await asyncio.wait_for(
                    _raw_query(system_prompt, instruction),
                    timeout=settings.llm_timeout_sec,
                )
                return schema.model_validate(_extract_json(text))
            except (LLMError, ValidationError, json.JSONDecodeError) as e:
                if attempt == 2:
                    raise LLMError(
                        f"{schema.__name__} not parseable after retry: {e}"
                    ) from e
                log.warning(
                    "%s parse failed (attempt %d): %s", schema.__name__, attempt, e
                )
                instruction += f"\n\nYour previous reply was invalid ({e}). Return only valid JSON."
            except TimeoutError as e:
                raise LLMError(
                    f"Claude timed out after {settings.llm_timeout_sec}s"
                ) from e
            except Exception as e:
                raise LLMError(f"Claude query failed: {e}") from e
    raise LLMError("unreachable")
