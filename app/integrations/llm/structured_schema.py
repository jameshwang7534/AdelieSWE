"""Prepare closed object schemas for strict OpenAI-compatible structured output."""

from typing import Any

from pydantic import BaseModel

from app.integrations.llm.provider import LLMError


def structured_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    if schema.get("type") != "object":
        raise LLMError("llm_invalid_configuration")

    def visit(node: dict[str, Any]) -> None:
        node.pop("default", None)
        if node.get("type") == "object":
            if node.get("additionalProperties") not in (None, False):
                raise LLMError("llm_invalid_configuration")
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        for key in ("properties", "$defs"):
            for child in node.get(key, {}).values():
                visit(child)
        if isinstance(node.get("items"), dict):
            visit(node["items"])
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in node.get(key, []):
                visit(child)

    visit(schema)
    return schema
