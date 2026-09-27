"""Versioned prompt templates for the triage workload (``prompts/*.yaml``)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PLACEHOLDER = "{input_text}"


class PromptTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_version: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    system: str = Field(min_length=1)
    user: str

    @field_validator("user")
    @classmethod
    def _one_placeholder(cls, v: str) -> str:
        if v.count(PLACEHOLDER) != 1:
            raise ValueError(f"user template must contain {PLACEHOLDER} exactly once")
        return v

    def messages(self, input_text: str) -> list[dict[str, str]]:
        # str.replace, not str.format: alert text contains JSON braces.
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user.replace(PLACEHOLDER, input_text)},
        ]


def load_prompt(path: str | Path) -> tuple[PromptTemplate, str]:
    """Return the template and the sha256 of the file bytes (recorded in the run manifest)."""
    path = Path(path)
    raw = path.read_bytes()
    try:
        prompt = PromptTemplate.model_validate(yaml.safe_load(raw.decode("utf-8")))
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"{path}: invalid prompt: {exc}") from exc
    return prompt, hashlib.sha256(raw).hexdigest()
