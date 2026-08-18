"""Test bootstrap.

Stubs `psycopg` so the control-flow tests can run without a live Postgres. These tests exist to
prove the runner's deterministic behaviour (gates, schema validation, context narrowing), none of
which touches the database — the memory layer is exercised separately against the real DB.
"""

import sys
import types

if "psycopg" not in sys.modules:
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        _pg = types.ModuleType("psycopg")
        _pg.connect = None
        sys.modules["psycopg"] = _pg
        _rows = types.ModuleType("psycopg.rows")
        _rows.dict_row = None
        sys.modules["psycopg.rows"] = _rows
