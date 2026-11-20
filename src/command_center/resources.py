"""Packaged resources (prompts, schemas, default configuration) via importlib.resources.

Nothing here depends on the current working directory or on a repository checkout: the
resources ship inside the wheel (see [tool.setuptools.package-data] in pyproject.toml).
Loaders accept either a filesystem Path (an organization's own config directory) or a
Traversable (the packaged defaults); both expose iterdir / read_text / name.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Union

Resource = Union[Path, Traversable]


def resource_root() -> Traversable:
    return files("command_center").joinpath("resources")


def resource(*parts: str) -> Traversable:
    node = resource_root()
    for p in parts:
        node = node.joinpath(p)
    return node


def read_text(*parts: str) -> str:
    return resource(*parts).read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def load_json_schema(name: str) -> dict[str, Any]:
    return json.loads(read_text("schemas", name))


def default_config_dir() -> Traversable:
    return resource("config")


def default_agent_dir() -> Traversable:
    return resource("config", "agents")


def default_policy_file() -> Traversable:
    return resource("config", "organization_policy.example.yaml")


def default_models_file() -> Traversable:
    return resource("config", "models.yaml")


def as_resource(path: str | Path | Traversable) -> Resource:
    return Path(path) if isinstance(path, (str, Path)) else path


def stem(node: Resource) -> str:
    name = node.name
    return name.rsplit(".", 1)[0] if "." in name else name
