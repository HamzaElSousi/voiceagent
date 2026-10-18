"""Starter agent configs for the dashboard's "new agent" form (copies of the examples, shipped as
package data so they exist inside the Docker image too)."""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import AgentConfig

_DIR = Path(__file__).resolve().parent / "agent_templates"


@lru_cache(maxsize=2)
def _load(mode: str) -> dict:
    raw = yaml.safe_load((_DIR / f"{mode}.yaml").read_text(encoding="utf-8"))
    # Round-trip through the model so the template is exactly what the API accepts back.
    return AgentConfig.from_dict(raw).model_dump(mode="json")


def agent_template(mode: str) -> dict:
    # Deep copy: callers edit nested sections, and must never mutate the cached template.
    return copy.deepcopy(_load(mode))
