from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class StreamEvent:
    pass


@dataclass
class TextDelta(StreamEvent):
    text: str


@dataclass
class ThinkingDelta(StreamEvent):
    text: str


@dataclass
class ToolUse(StreamEvent):
    tool: str
    path: str | None


@dataclass
class Result(StreamEvent):
    text: str
    session_id: str | None
    model: str | None


@dataclass
class Error(StreamEvent):
    message: str


def parse_stream_line(line: str) -> list[StreamEvent]:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Ignoring non-JSON stream line: %s", line[:100])
        return []

    event_type = data.get("type")

    if event_type == "result":
        if data.get("is_error"):
            return [Error(message=data.get("result", "Unknown error"))]
        model_usage = data.get("modelUsage", {})
        model = next(iter(model_usage), None)
        return [
            Result(
                text=data.get("result", ""),
                session_id=data.get("session_id"),
                model=model,
            )
        ]

    if event_type == "assistant":
        message = data.get("message", {})
        content = message.get("content", [])
        events: list[StreamEvent] = []
        for item in content:
            item_type = item.get("type")
            if item_type == "thinking":
                text = item.get("thinking", "")
                if text:
                    events.append(ThinkingDelta(text=text))
            elif item_type == "text":
                text = item.get("text", "")
                if text:
                    events.append(TextDelta(text=text))
            elif item_type == "tool_use":
                tool_name = item.get("name", "unknown")
                tool_input = item.get("input", {})
                file_path = tool_input.get("file_path")
                events.append(ToolUse(tool=tool_name, path=file_path))
        return events

    return []
