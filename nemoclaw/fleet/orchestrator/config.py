"""Loads and validates the NemoClaw agent / pipeline / schema registry.

Fails loudly at import time on a bad config. That is deliberate: a typo in an agent's
`tier` or `model` alias is a tenant-isolation or billing bug, and it should stop the
service starting rather than surface as odd behaviour under load.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FLEET_DIR = Path(os.getenv("NEMOCLAW_FLEET_DIR", Path(__file__).resolve().parent.parent))

# Tiers are the memory-isolation boundary. See agents.yaml.
TIER_CLIENT = "client"  # may touch client_kb + memory for the acting client
TIER_SKILL = "skill"  # may touch skill_kb ONLY — never client storage
VALID_TIERS = {TIER_CLIENT, TIER_SKILL}


@dataclass(frozen=True)
class Agent:
    id: str
    display_name: str
    tier: str
    model: str
    fallback: str | None
    prompt_path: Path
    mcp_tools: tuple[str, ...]
    description: str
    enabled: bool
    timeout_seconds: int
    max_retries: int

    @property
    def is_client_tier(self) -> bool:
        return self.tier == TIER_CLIENT

    def load_prompt(self) -> str:
        if not self.prompt_path.exists():
            raise FileNotFoundError(
                f"agent '{self.id}' declares prompt {self.prompt_path} which does not exist"
            )
        return self.prompt_path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Stage:
    id: str
    agent: str | None
    inputs: tuple[str, ...]
    output_schema: str | None
    gate: str | None
    on_fail: str
    approve_status: str | None = None
    reject_status: str | None = None
    rework_from: str | None = None

    @property
    def is_human_gate(self) -> bool:
        return self.gate == "human"


@dataclass(frozen=True)
class Pipeline:
    id: str
    description: str
    stages: tuple[Stage, ...]

    def stage(self, stage_id: str) -> Stage | None:
        return next((s for s in self.stages if s.id == stage_id), None)


@dataclass
class Registry:
    agents: dict[str, Agent] = field(default_factory=dict)
    pipelines: dict[str, Pipeline] = field(default_factory=dict)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)

    def agent(self, agent_id: str) -> Agent:
        try:
            return self.agents[agent_id]
        except KeyError:
            raise KeyError(
                f"unknown agent '{agent_id}'. known: {sorted(self.agents)}"
            ) from None

    def pipeline(self, pipeline_id: str) -> Pipeline:
        try:
            return self.pipelines[pipeline_id]
        except KeyError:
            raise KeyError(
                f"unknown pipeline '{pipeline_id}'. known: {sorted(self.pipelines)}"
            ) from None


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"required config missing: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def _parse_stage(raw: dict[str, Any], pipeline_id: str) -> Stage:
    # `on_reject: {rework_from: draft}` is the YAML shape used in pipelines.yaml
    on_reject = raw.get("on_reject") or {}
    if isinstance(on_reject, dict):
        rework_from = on_reject.get("rework_from")
    else:
        rework_from = None

    return Stage(
        id=raw["id"],
        agent=raw.get("agent"),
        inputs=tuple(raw.get("inputs") or ()),
        output_schema=raw.get("output_schema"),
        gate=raw.get("gate"),
        on_fail=raw.get("on_fail", "abort"),
        approve_status=raw.get("approve_status"),
        reject_status=raw.get("reject_status"),
        rework_from=rework_from,
    )


def load_registry(fleet_dir: Path | None = None) -> Registry:
    base = fleet_dir or FLEET_DIR
    agents_raw = _read_yaml(base / "agents.yaml")
    pipelines_raw = _read_yaml(base / "pipelines.yaml")
    schemas_raw = _read_yaml(base / "schemas.yaml")

    defaults = agents_raw.get("defaults") or {}
    reg = Registry(schemas=schemas_raw.get("schemas") or {})

    for agent_id, spec in (agents_raw.get("agents") or {}).items():
        tier = spec.get("tier")
        if tier not in VALID_TIERS:
            raise ValueError(
                f"agent '{agent_id}' has tier {tier!r}; must be one of {sorted(VALID_TIERS)}. "
                "This controls client-data access — refusing to guess."
            )
        reg.agents[agent_id] = Agent(
            id=agent_id,
            display_name=spec.get("display_name", agent_id),
            tier=tier,
            model=spec["model"],
            fallback=spec.get("fallback"),
            prompt_path=base / spec["prompt"],
            mcp_tools=tuple(spec.get("mcp_tools") or ()),
            description=(spec.get("description") or "").strip(),
            enabled=spec.get("enabled", True),
            timeout_seconds=spec.get("timeout_seconds", defaults.get("timeout_seconds", 180)),
            max_retries=spec.get("max_retries", defaults.get("max_retries", 1)),
        )

    for pipeline_id, spec in (pipelines_raw.get("pipelines") or {}).items():
        stages = tuple(_parse_stage(s, pipeline_id) for s in spec.get("stages") or ())
        reg.pipelines[pipeline_id] = Pipeline(
            id=pipeline_id,
            description=(spec.get("description") or "").strip(),
            stages=stages,
        )

    _validate(reg)
    return reg


def _validate(reg: Registry) -> None:
    """Cross-reference checks. Anything wrong here is a deploy-blocking bug."""
    problems: list[str] = []

    for p in reg.pipelines.values():
        seen: set[str] = set()
        for st in p.stages:
            if st.id in seen:
                problems.append(f"pipeline '{p.id}' has duplicate stage id '{st.id}'")
            seen.add(st.id)

            # A templated agent (e.g. "{{ task.specialist }}") is resolved at runtime.
            templated = bool(st.agent and "{{" in st.agent)
            if st.agent and not templated and st.agent not in reg.agents:
                problems.append(f"pipeline '{p.id}' stage '{st.id}' -> unknown agent '{st.agent}'")

            if st.output_schema and st.output_schema not in reg.schemas:
                problems.append(
                    f"pipeline '{p.id}' stage '{st.id}' -> unknown schema '{st.output_schema}'"
                )

            if st.rework_from and st.rework_from not in seen:
                problems.append(
                    f"pipeline '{p.id}' stage '{st.id}' reworks from '{st.rework_from}' "
                    "which is not an earlier stage"
                )

            # Every non-gate stage must produce something validatable, or we are back to
            # trusting free text from an open-weight model.
            if st.agent and not st.output_schema:
                problems.append(
                    f"pipeline '{p.id}' stage '{st.id}' has an agent but no output_schema"
                )

    for a in reg.agents.values():
        if not a.prompt_path.exists():
            problems.append(f"agent '{a.id}' prompt missing: {a.prompt_path}")
        # Specialists must not be granted client-data tools. Belt-and-braces alongside the
        # gateway-side MCP caps — this catches the config mistake before deploy.
        if a.tier == TIER_SKILL:
            for forbidden in ("client_kb", "postgres", "memory"):
                if forbidden in a.mcp_tools:
                    problems.append(
                        f"agent '{a.id}' is tier=skill but requests client-data tool "
                        f"'{forbidden}' — specialists must never touch client storage"
                    )

    if problems:
        raise ValueError("invalid fleet config:\n  - " + "\n  - ".join(problems))
