"""Built-in default prompt set."""

from __future__ import annotations

from pathlib import Path

from .registry import DEFAULT_PROMPT_SET_ID, register_prompt_set


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def register() -> None:
    extraction_prompt = (_plugin_root() / "prompts" / "extraction.txt").read_text(
        encoding="utf-8"
    )
    register_prompt_set(
        DEFAULT_PROMPT_SET_ID,
        {
            "llm.json_only": "Respond with JSON only. No explanation, no markdown fencing.",
            "ingest.extraction.system": extraction_prompt,
        },
        source=__name__,
    )
