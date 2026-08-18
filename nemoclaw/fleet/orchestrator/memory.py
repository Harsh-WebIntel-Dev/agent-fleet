"""The three memory tiers, with isolation enforced in code as well as at the DB.

    Hermes      -> memory      (per-client conversation)        tier=client
    PM          -> client_kb   (company facts, work done)        tier=client
    Specialists -> skill_kb    (craft only, client-agnostic)     tier=skill

WHY THE BELT AND BRACES: specialists are the SHARED tier — one Writer serves every client — which
makes them the single most likely cross-tenant leak path. Three independent things must all fail
before client data reaches a specialist:

  1. this module refuses the call            (ClientDataAccessDenied, below)
  2. LiteLLM per-agent MCP caps deny the tool (gateway, outside this process)
  3. Postgres RLS filters the rows           (`app.client_id`, db/migrations/001)

Postgres RLS is the backstop, not the plan. It only works if `SET app.client_id` was issued on the
same connection — so every client-scoped query here goes through `_client_session`, which sets it
inside the transaction and never hands out a raw connection.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

from .config import Agent

log = logging.getLogger("nemoclaw.memory")


class ClientDataAccessDenied(RuntimeError):
    """A tier=skill agent tried to reach client-scoped storage.

    This is a bug, not a runtime condition. It means either a config mistake or an attempt by a
    model to reach past its tier — both of which should be loud, never swallowed.
    """


@dataclass
class MemoryStore:
    dsn: str

    # ---------------------------------------------------------------- connections

    @contextlib.contextmanager
    def _session(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            yield conn

    @contextlib.contextmanager
    def _client_session(self, client_id: str) -> Iterator[psycopg.Connection]:
        """Connection with RLS armed for exactly one client.

        `SET LOCAL` scopes the setting to the transaction, so a pooled connection cannot leak the
        previous caller's client_id into the next query.
        """
        if not client_id:
            raise ValueError("client_id is required for client-scoped access")
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            with conn.transaction():
                conn.execute("SELECT set_config('app.client_id', %s, true)", (client_id,))
                yield conn

    # ---------------------------------------------------------------- guard

    @staticmethod
    def _require_client_tier(agent: Agent, what: str) -> None:
        if not agent.is_client_tier:
            raise ClientDataAccessDenied(
                f"agent '{agent.id}' is tier={agent.tier} and may not access {what}. "
                "Specialists receive client context as data from PM; they never query it. "
                "If this agent genuinely needs client data, that is an architecture change, "
                "not a config tweak."
            )

    # ---------------------------------------------------------------- tier: client

    def recall_conversation(
        self, agent: Agent, client_id: str, session_key: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Hermes: recent conversation turns for this client."""
        self._require_client_tier(agent, "conversation memory")
        with self._client_session(client_id) as conn:
            rows = conn.execute(
                """
                SELECT content, created_at
                  FROM memory.entries
                 WHERE session_key = %s
              ORDER BY created_at DESC
                 LIMIT %s
                """,
                (session_key, limit),
            ).fetchall()
        return list(reversed(rows))

    def remember_conversation(
        self, agent: Agent, client_id: str, session_key: str, content: str
    ) -> None:
        self._require_client_tier(agent, "conversation memory")
        with self._client_session(client_id) as conn:
            conn.execute(
                """
                INSERT INTO memory.entries (client_id, session_key, content)
                VALUES (%s, %s, %s)
                """,
                (client_id, session_key, content),
            )

    def recall_client_facts(
        self, agent: Agent, client_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """PM: what we know about this company and what work has been done.

        This is the source of e.g. "client A has a blog template" — PM reads it here and then
        INJECTS it into the specialist's request. The specialist never calls this.
        """
        self._require_client_tier(agent, "client_kb")
        with self._client_session(client_id) as conn:
            return conn.execute(
                """
                SELECT content, created_at
                  FROM client_kb.entries
              ORDER BY created_at DESC
                 LIMIT %s
                """,
                (limit,),
            ).fetchall()

    def remember_client_fact(self, agent: Agent, client_id: str, content: str) -> None:
        self._require_client_tier(agent, "client_kb")
        with self._client_session(client_id) as conn:
            conn.execute(
                "INSERT INTO client_kb.entries (client_id, content) VALUES (%s, %s)",
                (client_id, content),
            )

    def build_client_context(self, agent: Agent, client_id: str) -> dict[str, Any]:
        """The bundle PM injects into every specialist call.

        Everything a specialist knows about a client arrives through this dict and nothing else.
        Keys here are what pipelines.yaml refers to as `client.*`.
        """
        self._require_client_tier(agent, "client context")
        with self._client_session(client_id) as conn:
            row = conn.execute(
                "SELECT profile FROM app.clients WHERE id = %s", (client_id,)
            ).fetchone()
        profile = (row or {}).get("profile") or {}
        if isinstance(profile, str):
            profile = json.loads(profile)
        return profile

    # ---------------------------------------------------------------- tier: skill

    def recall_skill(self, agent: Agent, topic: str, limit: int = 10) -> list[dict[str, Any]]:
        """Any agent: shared, client-agnostic craft knowledge.

        No client_id anywhere in this path by construction — there is nothing to scope.
        """
        with self._session() as conn:
            return conn.execute(
                """
                SELECT content, promoted_at
                  FROM skill_kb.entries
                 WHERE content ILIKE %s
              ORDER BY promoted_at DESC
                 LIMIT %s
                """,
                (f"%{topic}%", limit),
            ).fetchall()

    def promote_to_skill(
        self, content: str, source_client_id: str | None, sanitised: bool
    ) -> None:
        """Write craft knowledge into the shared tier.

        `sanitised` is not a formality. Content reaching skill_kb is visible to every client, so
        promotion must go through kb/promotion_gate (which strips client identifiers) first. This
        refuses unsanitised writes outright rather than trusting the caller.
        """
        if not sanitised:
            raise ClientDataAccessDenied(
                "refusing to write unsanitised content into skill_kb — it is visible to every "
                "client. Route via kb/promotion_gate first."
            )
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO skill_kb.entries (source_client_id, content)
                VALUES (%s, %s)
                """,
                (source_client_id, content),
            )


def redact_for_specialist(context: dict[str, Any], allowed_keys: Sequence[str]) -> dict[str, Any]:
    """Narrow PM's client context to only the keys a given stage declared it needs.

    pipelines.yaml lists `inputs: [client.brand, client.blog_template]`. Passing the whole client
    profile "just in case" is how data ends up somewhere it shouldn't — so the runner passes only
    what was declared, and this is where that narrowing happens.
    """
    wanted = {k.split(".", 1)[1] for k in allowed_keys if k.startswith("client.")}
    missing = wanted - set(context)
    if missing:
        log.warning("client context missing declared keys: %s", sorted(missing))
    return {k: v for k, v in context.items() if k in wanted}
