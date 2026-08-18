"""Deterministic pipeline executor.

THIS FILE IS THE ANSWER TO THE OPEN-WEIGHT RELIABILITY PROBLEM.

The previous attempt (OpenClaw) failed on orchestration, not tooling: an open-weight model
confabulated capabilities it lacked, subagent results were silently lost, and multi-stage runs
stalled. Everything here follows from that post-mortem:

  * Sequencing is a Python loop over a declared stage list. The model never decides "what next".
  * Every stage output is validated against a JSON schema before the run advances. A stage that
    claims success but returns the wrong shape FAILS — it does not pass silently.
  * Progress is written to the LiteLLM workflow-run ledger after each stage, so a crash resumes
    instead of restarting.
  * Specialists get a narrowed client-context dict, never a DB handle.
  * A human gate genuinely blocks: the run is left PAUSED and the process returns.

If you find yourself wanting the model to choose the order of operations, that is the failure mode
this design exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import jsonschema

from .config import Agent, Pipeline, Registry, Stage
from .gateway import (
    BudgetExceeded,
    GatewayError,
    LiteLLMGateway,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RUNNING,
    parse_json_output,
)
from .memory import MemoryStore, redact_for_specialist
from .verification import VERIFIERS, UnverifiableClaim

log = logging.getLogger("nemoclaw.runner")


def _summarise(output: dict[str, Any], limit: int = 220) -> str:
    """One-line gist of a stage output for the human-readable run log.

    Prefers the fields a person actually wants to see; falls back to a truncated dump. Kept
    deliberately dumb — this is a log line, not a feature.
    """
    for key in ("title", "answer", "summary", "body", "url", "meta_title"):
        val = output.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:limit]
    if "passed" in output:  # qa_verdict
        reasons = "; ".join(output.get("reasons") or []) or "no reasons given"
        return f"{'PASS' if output['passed'] else 'FAIL'} — {reasons}"[:limit]
    return str(output)[:limit]


class AwaitingHuman(Exception):
    """Run paused at a human gate. Not an error — the expected way an approval stage ends."""

    def __init__(self, run_id: str, stage_id: str):
        super().__init__(f"run {run_id} awaiting human approval at stage '{stage_id}'")
        self.run_id = run_id
        self.stage_id = stage_id


class StageFailed(Exception):
    def __init__(self, stage_id: str, reason: str):
        super().__init__(f"stage '{stage_id}' failed: {reason}")
        self.stage_id = stage_id
        self.reason = reason


class QAGateFailed(StageFailed):
    """QA returned passed=false. Publishing must not proceed."""


class ToolUnavailable(StageFailed):
    """A stage needs an MCP server that is not registered. Fail rather than let it improvise."""


@dataclass
class RunState:
    run_id: str
    client_id: str
    pipeline_id: str
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "pipeline": self.pipeline_id,
            "completed_stages": list(self.completed),
            "outputs": self.outputs,
        }


@dataclass
class PipelineRunner:
    registry: Registry
    gateway: LiteLLMGateway
    memory: MemoryStore
    pm_agent_id: str = "pm"
    # Injected so tests can pin it and so an unreachable gateway fails CLOSED (empty set = no
    # tools available = every side-effect stage refuses to run).
    available_tools: Callable[[], set[str]] | None = None

    def _persist(self, state: RunState, status: str, paused_stage: str | None = None) -> None:
        """Mirror run state into app.runs. Best-effort; MemoryStore swallows its own failures."""
        saver = getattr(self.memory, "save_run", None)
        if saver is None:
            return
        saver(
            run_id=state.run_id,
            client_id=state.client_id,
            pipeline=state.pipeline_id,
            status=status,
            paused_stage=paused_stage,
            resume_state=state.snapshot(),
        )

    def _tools_now(self) -> set[str]:
        if self.available_tools is None:
            return set()
        try:
            return set(self.available_tools())
        except Exception:  # noqa: BLE001 - unreachable gateway means "nothing is available"
            log.warning("MCP tool probe failed; treating all tools as unavailable")
            return set()

    # ------------------------------------------------------------------ inputs

    def _resolve_inputs(
        self, stage: Stage, state: RunState, task: dict[str, Any], client_ctx: dict[str, Any]
    ) -> dict[str, Any]:
        """Turn `inputs: [research.output, client.brand, task.topic]` into a concrete dict.

        Only declared keys are passed. A stage that didn't declare it doesn't see it — which is
        how client data stays contained even though every specialist shares one process.
        """
        resolved: dict[str, Any] = {}
        for ref in stage.inputs:
            head, _, rest = ref.partition(".")

            if head == "task":
                if rest in task:
                    resolved[ref] = task[rest]
                continue

            if head == "client":
                continue  # handled in bulk below, via redact_for_specialist

            prior = state.outputs.get(head)
            if prior is None:
                log.warning("stage '%s' wants '%s' but %s has no output", stage.id, ref, head)
                continue
            # `research.output` -> whole dict; `draft.output.title` -> one field
            value: Any = prior
            for part in rest.split(".")[1:] if rest.startswith("output") else []:
                if isinstance(value, dict):
                    value = value.get(part)
            resolved[ref] = value if rest != "output" else prior

        narrowed = redact_for_specialist(client_ctx, stage.inputs)
        for k, v in narrowed.items():
            resolved[f"client.{k}"] = v
        return resolved

    # ------------------------------------------------------------------ one stage

    def _run_agent_stage(
        self,
        stage: Stage,
        agent: Agent,
        payload: dict[str, Any],
        state: RunState,
        client_key: str,
    ) -> dict[str, Any]:
        schema = self.registry.schemas.get(stage.output_schema or "")
        system_prompt = agent.load_prompt()

        user_msg = (
            "Inputs for this stage (JSON):\n"
            f"{payload}\n\n"
            "Respond with ONLY a JSON object matching the required schema. No prose, no fences."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

        attempts = agent.max_retries + 1
        last_err: str | None = None

        for attempt in range(1, attempts + 1):
            model = agent.model if attempt == 1 else (agent.fallback or agent.model)
            try:
                res = self.gateway.complete(
                    model=model,
                    messages=messages,
                    client_key=client_key,
                    response_schema=schema,
                    session_id=state.run_id,
                    timeout=agent.timeout_seconds,
                )
                parsed = parse_json_output(res["content"])

                if schema:
                    # Validate, don't trust. This is what catches "I uploaded the image" when no
                    # image exists — the schema demands a URL and real dimensions.
                    jsonschema.validate(parsed, schema)

                self.gateway.record(
                    state.run_id,
                    event_type="stage_completed",
                    step_name=stage.id,
                    data={"agent": agent.id, "model": model, "attempt": attempt,
                          "usage": res.get("usage")},
                )
                # Human-readable trail alongside the machine events, so the run is legible in the
                # LiteLLM UI without digging through event payloads.
                self.gateway.say(
                    state.run_id,
                    role="assistant",
                    content=f"[{stage.id}] {agent.display_name} ({model}) completed: "
                            f"{_summarise(parsed)}",
                )
                return parsed

            except BudgetExceeded:
                # Never retry a budget failure — it will not pass, and retrying burns nothing but
                # time while hiding the real cause from the human.
                self.gateway.record(
                    state.run_id, event_type="budget_exceeded", step_name=stage.id
                )
                raise

            except (GatewayError, ValueError, jsonschema.ValidationError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "stage '%s' attempt %s/%s failed (%s)", stage.id, attempt, attempts, last_err
                )
                self.gateway.record(
                    state.run_id,
                    event_type="stage_attempt_failed",
                    step_name=stage.id,
                    data={"attempt": attempt, "model": model, "error": last_err[:500]},
                )

        raise StageFailed(stage.id, last_err or "unknown error")

    # ------------------------------------------------------------------ the loop

    def run(
        self,
        *,
        pipeline_id: str,
        client_id: str,
        task: dict[str, Any],
        client_key: str,
        resume_run_id: str | None = None,
        resume_state: RunState | None = None,
        start_after: str | None = None,
    ) -> RunState:
        pipeline: Pipeline = self.registry.pipeline(pipeline_id)
        pm = self.registry.agent(self.pm_agent_id)

        # PM is the only tier=client agent in this path; it reads client_kb and injects the
        # result. Specialists below receive a narrowed copy and nothing else.
        client_ctx = self.memory.build_client_context(pm, client_id)

        if resume_state:
            state = resume_state
        else:
            run_id = resume_run_id or self.gateway.open_run(
                workflow_type=pipeline_id, task_input={"client_id": client_id, **task}
            )
            state = RunState(run_id=run_id, client_id=client_id, pipeline_id=pipeline_id)

        self.gateway.close_run(state.run_id, status=RUN_RUNNING)
        self._persist(state, RUN_RUNNING)

        skipping = start_after is not None
        for stage in pipeline.stages:
            if skipping:
                if stage.id == start_after:
                    skipping = False
                continue
            if stage.id in state.completed:
                continue

            # ---- human gate: stop the world, leave it resumable
            if stage.is_human_gate:
                self.gateway.record(
                    state.run_id,
                    event_type="awaiting_approval",
                    step_name=stage.id,
                    data={"approve_status": stage.approve_status,
                          "reject_status": stage.reject_status},
                )
                self.gateway.say(
                    state.run_id,
                    role="system",
                    content=f"[{stage.id}] PAUSED — awaiting human approval. "
                            f"Set ClickUp status to '{stage.approve_status}' to continue "
                            f"or '{stage.reject_status}' to send back for rework.",
                )
                self.gateway.close_run(
                    state.run_id, status=RUN_PAUSED, output=state.snapshot()
                )
                self._persist(state, RUN_PAUSED, paused_stage=stage.id)
                raise AwaitingHuman(state.run_id, stage.id)

            agent_id = stage.agent
            if agent_id and "{{" in agent_id:
                # e.g. consult pipeline: "{{ task.specialist }}"
                key = agent_id.strip("{} ").split(".", 1)[1]
                agent_id = task.get(key)
                if not agent_id:
                    raise StageFailed(stage.id, f"templated agent unresolved (task.{key} missing)")

            agent = self.registry.agent(agent_id)
            if not agent.enabled:
                raise StageFailed(stage.id, f"agent '{agent.id}' is disabled in agents.yaml")

            # ---- HARD PRE-FLIGHT: refuse a side-effect stage whose tools do not exist.
            # The model is never called, so it never gets the chance to narrate a success it had
            # no means to achieve. This is deterministic; no judgement involved.
            if stage.requires_tools:
                have = self._tools_now()
                missing = [t for t in stage.requires_tools if t not in have]
                if missing:
                    reason = (
                        f"required MCP server(s) not registered: {', '.join(missing)} "
                        f"(available: {', '.join(sorted(have)) or 'none'})"
                    )
                    self.gateway.record(
                        state.run_id, event_type="tool_unavailable", step_name=stage.id,
                        data={"missing": missing, "available": sorted(have)},
                    )
                    self.gateway.say(
                        state.run_id, role="system",
                        content=f"[{stage.id}] BLOCKED before running — {reason}",
                    )
                    self.gateway.close_run(
                        state.run_id, status=RUN_FAILED, output=state.snapshot()
                    )
                    self._persist(state, RUN_FAILED, paused_stage=stage.id)
                    raise ToolUnavailable(stage.id, reason)

            payload = self._resolve_inputs(stage, state, task, client_ctx)

            try:
                output = self._run_agent_stage(stage, agent, payload, state, client_key)
            except StageFailed:
                if stage.on_fail == "skip":
                    log.warning("stage '%s' failed but on_fail=skip", stage.id)
                    continue
                self.gateway.close_run(state.run_id, status=RUN_FAILED, output=state.snapshot())
                self._persist(state, RUN_FAILED, paused_stage=stage.id)
                raise

            # ---- INDEPENDENT VERIFICATION: go and check the claimed side effect ourselves.
            # Schema validation proved the SHAPE was right. This proves the CLAIM was true. The
            # check deliberately lives here and not in the agent that made the claim.
            if stage.verify:
                verifier = VERIFIERS[stage.verify]
                result = verifier(output)
                self.gateway.record(
                    state.run_id,
                    event_type="verification_passed" if result.ok else "verification_failed",
                    step_name=stage.id,
                    data={"verifier": stage.verify, "reason": result.reason,
                          "evidence": result.evidence},
                )
                if not result.ok:
                    self.gateway.say(
                        state.run_id, role="system",
                        content=f"[{stage.id}] VERIFICATION FAILED — the stage reported success "
                                f"but it could not be confirmed: {result.reason}",
                    )
                    self.gateway.close_run(
                        state.run_id, status=RUN_FAILED, output=state.snapshot()
                    )
                    self._persist(state, RUN_FAILED, paused_stage=stage.id)
                    raise UnverifiableClaim(stage.id, result)
                self.gateway.say(
                    state.run_id, role="system",
                    content=f"[{stage.id}] verified independently: {result.reason}",
                )

            # ---- PM backstop gate for pipelines with no QA stage. Same enforcement shape as QA:
            # a boolean read by code, not a judgement the next stage can talk past.
            if stage.gate == "pm_must_approve" and not output.get("approved_for_user", False):
                reasons = output.get("concerns") or ["PM did not approve for user"]
                self.gateway.record(
                    state.run_id, event_type="pm_review_rejected", step_name=stage.id,
                    data={"concerns": reasons, "unverified": output.get("unverified_claims")},
                )
                self.gateway.say(
                    state.run_id, role="system",
                    content=f"[{stage.id}] PM REVIEW REJECTED — not returned to user. "
                            + "; ".join(reasons),
                )
                self.gateway.close_run(state.run_id, status=RUN_FAILED, output=state.snapshot())
                self._persist(state, RUN_FAILED, paused_stage=stage.id)
                raise StageFailed(stage.id, "; ".join(reasons))

            # Surface anything PM flagged as unverifiable even when it approved — the user should
            # see "I could not confirm X" rather than a clean-looking result.
            if stage.gate == "pm_must_approve" and output.get("unverified_claims"):
                self.gateway.say(
                    state.run_id, role="system",
                    content=f"[{stage.id}] PM flagged UNVERIFIED claims: "
                            + "; ".join(output["unverified_claims"]),
                )

            # ---- QA is a hard gate, evaluated by code not by the next agent's goodwill
            if stage.gate == "qa_must_pass" and not output.get("passed", False):
                self.gateway.record(
                    state.run_id,
                    event_type="qa_failed",
                    step_name=stage.id,
                    data={"reasons": output.get("reasons")},
                )
                self.gateway.say(
                    state.run_id,
                    role="system",
                    content=f"[{stage.id}] QA GATE FAILED — publishing blocked. "
                            + "; ".join(output.get("reasons") or ["no reasons given"]),
                )
                self.gateway.close_run(state.run_id, status=RUN_FAILED, output=state.snapshot())
                self._persist(state, RUN_FAILED, paused_stage=stage.id)
                raise QAGateFailed(stage.id, "; ".join(output.get("reasons") or ["QA failed"]))

            state.outputs[stage.id] = output
            state.completed.append(stage.id)

        self.gateway.close_run(state.run_id, status=RUN_COMPLETED, output=state.snapshot())
        self._persist(state, RUN_COMPLETED)
        return state

    # ------------------------------------------------------------------ resumption

    def resume_after_approval(
        self, *, state: RunState, approved: bool, client_key: str, task: dict[str, Any]
    ) -> RunState:
        """Continue a run paused at a human gate.

        approved -> carry on from the stage after the gate.
        rejected -> restart from the stage named in `on_reject.rework_from`.
        """
        pipeline = self.registry.pipeline(state.pipeline_id)
        gate = next((s for s in pipeline.stages if s.is_human_gate), None)
        if gate is None:
            raise ValueError(f"pipeline '{state.pipeline_id}' has no human gate to resume from")

        if approved:
            state.completed.append(gate.id)
            return self.run(
                pipeline_id=state.pipeline_id,
                client_id=state.client_id,
                task=task,
                client_key=client_key,
                resume_state=state,
                start_after=gate.id,
            )

        rework_from = gate.rework_from
        if not rework_from:
            self.gateway.close_run(state.run_id, status=RUN_FAILED, output=state.snapshot())
            raise StageFailed(gate.id, "rejected by human and no rework_from declared")

        # Discard everything from the rework point onwards so it is genuinely redone.
        idx = [s.id for s in pipeline.stages].index(rework_from)
        for st in pipeline.stages[idx:]:
            state.outputs.pop(st.id, None)
            if st.id in state.completed:
                state.completed.remove(st.id)

        self.gateway.record(
            state.run_id, event_type="rework", step_name=gate.id,
            data={"restart_from": rework_from},
        )
        return self.run(
            pipeline_id=state.pipeline_id,
            client_id=state.client_id,
            task=task,
            client_key=client_key,
            resume_state=state,
        )
