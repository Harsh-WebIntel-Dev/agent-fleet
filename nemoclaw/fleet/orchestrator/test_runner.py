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

import jsonschema
import pytest

from .config import load_registry
from .memory import ClientDataAccessDenied
from .runner import (
    AwaitingHuman,
    RunState,
    PipelineRunner,
    QAGateFailed,
    StageFailed,
    ToolUnavailable,
)


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
    # Existing tests predate requires_tools; give them every tool so they keep testing what they
    # were written to test. The tool-gate has its own dedicated test below.
    r = PipelineRunner(
        registry=registry, gateway=gw, memory=mem,
        available_tools=lambda: {'wordpress', 'gmb', 'higgsfield', 'postiz'},
    )

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


def test_pm_review_rejection_blocks_output_reaching_the_user(registry):
    """Work that skips QA must be stopped by PM, not merely commented on."""
    r, gw = _runner(
        registry,
        {
            "answer": {"answer": "Scheduled 3 posts via Postiz.", "confidence": "high"},
            "pm_review": {
                "approved_for_user": False,
                "summary": "Specialist claims scheduling it could not have performed.",
                "unverified_claims": ["'Scheduled 3 posts via Postiz' — Postiz is unavailable"],
                "concerns": ["fabricated tool use"],
            },
        },
    )
    with pytest.raises(StageFailed):
        r.run(
            pipeline_id="consult",
            client_id="c1",
            task={"specialist": "social", "question": "plan social"},
            client_key="sk-x",
        )
    assert ("pm_review_rejected", "pm_review") in gw.events


def test_pm_review_surfaces_unverified_claims_even_when_approved(registry):
    """An approved-but-caveated result must still tell the user what couldn't be confirmed."""
    r, gw = _runner(
        registry,
        {
            "answer": {"answer": "Here is a draft caption.", "confidence": "medium"},
            "pm_review": {
                "approved_for_user": True,
                "summary": "Draft caption only; nothing was scheduled.",
                "unverified_claims": ["no Postiz tool available, so nothing was scheduled"],
                "concerns": [],
            },
        },
    )
    r.run(
        pipeline_id="consult",
        client_id="c1",
        task={"specialist": "social", "question": "plan social"},
        client_key="sk-x",
    )
    joined = " ".join(c for _, c in gw.messages)
    assert "UNVERIFIED" in joined, gw.messages


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


# --------------------------------------------------------------- anti-confabulation

def test_side_effect_stage_refuses_to_run_when_its_tool_is_absent(registry):
    """The strongest control: no tool -> the model is never called, so it cannot invent success.

    Reproduces the live 2026-08-18 failure, where `publisher` returned a complete, schema-valid
    publish_result with no WordPress MCP server in existence.
    """
    r, gw = _runner(registry, dict(GOOD))
    r.available_tools = lambda: set()  # nothing registered, as is the case today

    # gmb_post pauses for human approval first — and the live fabrication happened on exactly
    # this path, after a human approved. So resume through the gate, as the real run did.
    with pytest.raises(AwaitingHuman) as paused:
        r.run(pipeline_id="gmb_post", client_id="c1", task={}, client_key="sk-x")
    state = RunState(
        run_id=paused.value.run_id, client_id="c1", pipeline_id="gmb_post",
        outputs={"compose": GOOD["compose"], "qa": GOOD["qa"]},
        completed=["compose", "qa"],
    )

    with pytest.raises(ToolUnavailable) as ex:
        r.resume_after_approval(state=state, approved=True, client_key="sk-x", task={})
    # compose needs no tools, so it ran; publish needs `gmb`, which does not exist.
    assert ex.value.stage_id == "publish"
    assert "gmb" in ex.value.reason
    # the publisher model was never invoked
    assert "publish" not in gw.seen_payloads
    assert ("tool_unavailable", "publish") in gw.events


def test_fabricated_publish_url_is_caught_by_independent_verification(registry):
    """The exact fabricated output observed live must not survive verification.

    Schema validation passed it. Only fetching the URL catches it.
    """
    from .verification import verify_publish_result

    fabricated = {
        "published": True,
        "target": "gmb",
        "remote_id": "campaign_b0cb6d80_0132_root-post",
        "url": "https://www.google.com/search?q=demo+co+melbourne&ibnd=R0CB6D80-0132-Root-Post1",
    }
    # it is schema-valid — that is the whole problem
    jsonschema.validate(fabricated, registry.schemas["publish_result"])

    result = verify_publish_result(fabricated)
    assert result.ok is False
    assert "search" in result.reason.lower()


def test_verification_rejects_a_url_pointing_back_inside_our_own_network(registry):
    """A fabricated internal URL would otherwise 200 from our own service and 'verify'."""
    from .verification import verify_publish_result

    for url in ("http://localhost:8000/healthz", "http://127.0.0.1/post/1",
                "http://10.0.1.5:3000/x", "https://example.com/blog/post"):
        result = verify_publish_result(
            {"published": True, "target": "wordpress", "remote_id": "42", "url": url}
        )
        assert result.ok is False, f"{url} was wrongly accepted"


def test_claimed_image_dimensions_are_checked_against_real_bytes():
    """`width: 1920` is as easy to invent as anything else — decode the header instead."""
    import struct as _struct
    from .verification import _image_dimensions

    # a minimal real PNG header claiming 800x600
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0d" + b"IHDR" + _struct.pack(">II", 800, 600)
    assert _image_dimensions(png) == (800, 600)
    assert _image_dimensions(b"not an image at all") is None


def test_config_rejects_a_side_effect_stage_with_no_verification(tmp_path, registry):
    """Nobody can add an unverified publish stage later without the loader refusing to start."""
    import shutil
    from .config import FLEET_DIR, load_registry

    shutil.copytree(FLEET_DIR, tmp_path / "fleet", dirs_exist_ok=True)
    pipelines = tmp_path / "fleet" / "pipelines.yaml"
    pipelines.write_text(
        pipelines.read_text()
        .replace("        requires_tools: [gmb]\n", "")
        .replace("        verify: publish_result\n        on_fail: abort\n\n  # A single",
                 "        on_fail: abort\n\n  # A single")
    )
    with pytest.raises(ValueError) as ex:
        load_registry(tmp_path / "fleet")
    msg = str(ex.value)
    assert "requires_tools" in msg or "no verify" in msg
