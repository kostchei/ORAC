from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any


@dataclass(frozen=True)
class AgentProfile:
    slug: str
    name: str
    purpose: str
    system_prompt: str
    protocol_file: str
    tools: list[str]
    order: int
    kind: str = "council"  # "council" = review-loop agent; "doer" = subagent (e.g. Builder)


@dataclass(frozen=True)
class AgentProtocolStep:
    step: int
    name: str
    description: str
    details: str | None = None


@dataclass(frozen=True)
class AgentProtocolSpec:
    title: str
    mission: str
    response_prompt: str
    steps: tuple[AgentProtocolStep, ...]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    regular_use: str
    inputs: list[str]


def _read_package_text(relative_path: str) -> str:
    package_root = files("orac")
    return package_root.joinpath(relative_path).read_text(encoding="utf-8")


def _read_json(relative_path: str) -> Any:
    return json.loads(_read_package_text(relative_path))


def load_agent_profiles() -> list[AgentProfile]:
    manifest = _read_json("prompts/agents.json")
    profiles: list[AgentProfile] = []
    for item in manifest["agents"]:
        prompt = _read_package_text(f"prompts/{item['prompt_file']}").strip()
        profiles.append(
            AgentProfile(
                slug=str(item["slug"]),
                name=str(item["name"]),
                purpose=str(item["purpose"]),
                system_prompt=prompt,
                protocol_file=str(item["protocol_file"]),
                tools=list(item.get("tools", [])),
                order=int(item["order"]),
                kind=str(item.get("kind", "council")),
            )
        )
    return sorted(profiles, key=lambda profile: profile.order)


def load_tool_specs() -> list[ToolSpec]:
    manifest = _read_json("tools/catalog.json")
    return [
        ToolSpec(
            name=str(item["name"]),
            description=str(item["description"]),
            regular_use=str(item["regular_use"]),
            inputs=list(item.get("inputs", [])),
        )
        for item in manifest["tools"]
    ]


def get_tool_map() -> dict[str, ToolSpec]:
    return {tool.name: tool for tool in load_tool_specs()}


def load_agent_protocol(protocol_file: str) -> AgentProtocolSpec:
    data = _read_json(f"prompts/{protocol_file}")
    steps = tuple(
        AgentProtocolStep(
            step=int(item["step"]),
            name=str(item["name"]),
            description=str(item["description"]),
            details=str(item["details"]) if "details" in item else None,
        )
        for item in data["protocol"]
    )
    return AgentProtocolSpec(
        title=str(data["title"]),
        mission=str(data["mission"]),
        response_prompt=str(data["response_prompt"]),
        steps=steps,
    )


def get_agent_protocol(agent_slug: str) -> AgentProtocolSpec:
    profiles = {profile.slug: profile for profile in load_agent_profiles()}
    try:
        profile = profiles[agent_slug]
    except KeyError as exc:
        raise KeyError(f"No agent found for {agent_slug!r}.") from exc
    return load_agent_protocol(profile.protocol_file)
