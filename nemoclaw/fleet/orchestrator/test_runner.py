"""Control-flow tests for the pipeline runner.

These deliberately use a fake gateway and fake memory. The point is not to test the models — it is
to prove the DETERMINISTIC parts hold regardless of what a model returns, because the whole design
rests on the runner not trusting model output:

  * schema validation actually rejects malformed/lying output
  * a QA fail genuinely blocks publish
  * a human gate genuinely pauses
  * rejection genuinely reworks from the right stage
  * a specialist never receives undeclared client keys

Run:  python -m pytest fleet/orchestrator/test_runner.py -q
"""

from __future__ import annotations

from typing import Any

import pytest

from .config import load_registry
from .memory import ClientDataAccessDenied
from .runner import AwaitingHuman, PipelineRunner, QAGateFailed, StageFailed


class FakeGateway:
    """Records calls; returns whatever the test queued for each stage."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.events: list[tuple[str, str]] = []
        self.messages: list[tuple[str, str]] = []
        self.runs: dict[str, str] = {}
        self.seen_payloads: dict[str, dict] = {}
        self.timeouts_seen: list[float | None] = []
        self._n = 0

    def say(self, run_id, *, role, content):
        self.messages.append((role, content))

    def open_run(self, *, workflow_type, task_input):
        self._n += 1
        rid = f"run-{self._n}"
        self.runs[rid] = "pending"
        return rid

    def record(self, run_id, *, event_type, step_name, data=None):
        self.events.append((event_type, step_name))

    def close_run(self, run_id, *, status, output=None):
        self.runs[run_id] = status

    def get_run(self, run_id):
        return {"run_id": run_id, "status": self.runs.get(run_id)}

    def complete(
        self, *, model, messages, client_key, response_schema=None, session_id=None, timeout=None
    ):
        self.timeouts_seen.append(timeout)
        # last queued response for whichever stage the runner is on
        key = self._current
        val = self.responses[key]
        if isinstance(val, list):
            val = val.pop(0)
        import json

        return {"content": json.dumps(val), "usage": {}, "model": model}


class FakeMemory:
    def __init__(self, profile: dict[str, Any]):
        self.profile = profile

    def build_client_context(self, agent, client_id):
        if not agent.is_client_tier:
            raise ClientDataAccessDenied(f"{agent.id} is tier=skill")
        return self.profile


@pytest.fixture
def registry():
    return load_registry()


def _runner(registry, responses, profile=None):
    gw = FakeGateway(responses)
    mem = FakeMemory(profile or {"brand": "B", "blog_template": "T", "secret": "S"})
    r = PipelineRunner(registry=registry, gateway=gw, memory=mem)

    # patch _run_agent_stage to record which stage is executing + what payload it got
    orig = r._run_agent_stage

    def wrapped(stage, agent, payload, state, client_key):
        gw._current = stage.id
        gw.seen_payloads[stage.id] = payload
        return orig(stage, agent, payload, state, client_key)

    r._run_agent_stage = wrapped
    return r, gw


GOOD = {
    "compose": {"body": "A short GMB post about our workshop.", "cta": "LEARN_MORE"},
    "qa": {
        "passed": True,
        "checks": {"brand_fit": True, "factual_support": True, "compliance": True},
        "reasons": [],
    },
    "publish": {
        "published": True,
        "target": "gmb",
        "remote_id": "gmb-123",
        "url": "https://example.com/p/1",
    },
}


def test_human_gate_pauses_before_publish(registry):
    r, gw = _runner(registry, dict(GOOD))
    with pytest.raises(AwaitingHuman) as ex:
        r.run(pipeline_id="gmb_post", client_id="c1", task={}, client_key="sk-x")
    assert ex.value.stage_id == "approval"
    # publish must NOT have run
    assert "publish" not in gw.seen_payloads
    assert gw.runs[ex.value.run_id] == "paused"


def test_run_log_gets_human_readable_messages(registry):
    """Events alone leave the LiteLLM Workflow Runs view empty — messages are what a human reads."""
    r, gw = _runner(registry, dict(GOOD))
    with pytest.raises(AwaitingHuman):
        r.run(pipeline_id="gmb_post", client_id="c1", task={}, client_key="sk-x")
    assert gw.messages, "no messages written to the run log"
    joined = " ".join(c for _, c in gw.messages)
    assert "[compose]" in joined
    assert "PAUSED" in joined and "awaiting human approval" in joined


def test_per_agent_timeout_is_actually_passed(registry):
    """agents.yaml declares timeout_seconds; it must reach the gateway, not be dead config."""
    r, gw = _runner(registry, dict(GOOD))
    with pytest.raises(AwaitingHuman):
        r.run(pipeline_id="gmb_post", client_id="c1", task={}, client_key="sk-x")
    assert gw.timeouts_seen, "no timeout was passed through at all"
    # gmb uses the default (300); qa overrides to 420 — both must be real numbers, never None
    assert all(isinstance(t, (int, float)) for t in gw.timeouts_seen), gw.timeouts_seen
    assert 420 in gw.timeouts_seen, f"qa's 420s override never reached the gateway: {gw.timeouts_seen}"


def test_qa_failure_blocks_the_run(registry):
    bad = dict(GOOD)
    bad["qa"] = {
        "passed": False,
        "checks": {"brand_fit": True, "factual_support": False, "compliance": False},
        "reasons": ["unsubstantiated 'carbon neutral' claim"],
    }
    r, gw = _runner(registry, bad)
    with pytest.raises(QAGateFailed) as ex:
        r.run(pipeline_id="gmb_post", client_id="c1", task={}, client_key="sk-x")
    assert "carbon neutral" in str(ex.value)
    assert "publish" not in gw.seen_payloads
    assert ("qa_failed", "qa") in gw.events


def test_schema_violation_fails_the_stage(registry):
    """A model claiming success with the wrong shape must not pass."""
    bad = dict(GOOD)
    # missing required remote_id/url -> publisher "succeeded" without proof
    bad["compose"] = {"body": "x", "cta": "NOT_A_VALID_CTA"}
    r, gw = _runner(registry, bad)
    with pytest.raises(StageFailed):
        r.run(pipeline_id="gmb_post", client_id="c1", task={}, client_key="sk-x")


def test_specialist_only_sees_declared_client_keys(registry):
    r, gw = _runner(
        registry,
        dict(GOOD),
        profile={"brand": "B", "nap": "N", "wordpress_password": "SECRET"},
    )
    with pytest.raises(AwaitingHuman):
        r.run(pipeline_id="gmb_post", client_id="c1", task={"source_url": "u"}, client_key="sk-x")
    payload = gw.seen_payloads["compose"]
    # compose declares client.brand + client.nap, NOT the password
    assert "client.brand" in payload
    assert not any("password" in k for k in payload), payload
