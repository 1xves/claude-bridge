"""
Usage Logger — claude-bridge
─────────────────────────────
SQLite-based logger that records every API call made through the bridge.
Tracks tokens, estimated cost, duration, and caller identity so you can
attribute usage across your team without relying on provider dashboards.

Cost rates are estimates based on published pricing and will drift over time.
Update COST_PER_1K_TOKENS if OpenAI or Anthropic changes their pricing.
"""

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Cost estimates (USD per 1,000 tokens) ─────────────────────────────────────
# Update these when pricing changes. These are approximations.
COST_PER_1K_TOKENS: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o":           {"input": 0.0025, "output": 0.010},
    "gpt-4o-mini":      {"input": 0.00015, "output": 0.0006},
    "gpt-4.1":          {"input": 0.002,  "output": 0.008},
    "o3":               {"input": 0.010,  "output": 0.040},
    "o4-mini":          {"input": 0.0011, "output": 0.0044},
    # Anthropic
    "claude-opus-4-6":   {"input": 0.015, "output": 0.075},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5-20251001":  {"input": 0.00025, "output": 0.00125},
    # Fallback for unknown models
    "_default":         {"input": 0.005, "output": 0.015},
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a model call."""
    # Match by prefix if exact model not found
    rates = COST_PER_1K_TOKENS.get(model)
    if rates is None:
        for key in COST_PER_1K_TOKENS:
            if key != "_default" and model.startswith(key):
                rates = COST_PER_1K_TOKENS[key]
                break
    if rates is None:
        rates = COST_PER_1K_TOKENS["_default"]
    return (input_tokens / 1000 * rates["input"]) + (output_tokens / 1000 * rates["output"])


# ── Database setup ────────────────────────────────────────────────────────────

def _db_path() -> Path:
    """Return the SQLite DB path from env or default."""
    env = os.environ.get("BRIDGE_LOG_DB")
    if env:
        return Path(env)
    return Path(__file__).parent / "usage.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the usage_log table if it doesn't exist."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_log (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp               TEXT    NOT NULL,
                endpoint                TEXT    NOT NULL,
                caller_id               TEXT,
                domain                  TEXT,
                proposer_backend        TEXT,
                proposer_model          TEXT,
                challenger_backend      TEXT,
                challenger_model        TEXT,
                rounds                  INTEGER,
                anthropic_input_tokens  INTEGER,
                anthropic_output_tokens INTEGER,
                openai_input_tokens     INTEGER,
                openai_output_tokens    INTEGER,
                estimated_cost_usd      REAL,
                duration_seconds        REAL,
                success                 INTEGER NOT NULL DEFAULT 1,
                error_message           TEXT
            )
        """)
        conn.commit()


# ── Context manager for timing + logging ─────────────────────────────────────

class CallLogger:
    """
    Context manager that times a bridge call and writes a log record.

    Usage:
        with CallLogger(endpoint="/ask", caller_id=caller) as call:
            result = do_the_work()
            call.anthropic_tokens(input=100, output=200)
            call.openai_tokens(input=50, output=100)
        # Record is written on __exit__
    """

    def __init__(
        self,
        endpoint: str,
        caller_id: Optional[str] = None,
        domain: Optional[str] = None,
        proposer_backend: Optional[str] = None,
        proposer_model: Optional[str] = None,
        challenger_backend: Optional[str] = None,
        challenger_model: Optional[str] = None,
        rounds: Optional[int] = None,
    ):
        self.endpoint          = endpoint
        self.caller_id         = caller_id
        self.domain            = domain
        self.proposer_backend  = proposer_backend
        self.proposer_model    = proposer_model
        self.challenger_backend = challenger_backend
        self.challenger_model  = challenger_model
        self.rounds            = rounds

        self._ant_in:   int   = 0
        self._ant_out:  int   = 0
        self._oai_in:   int   = 0
        self._oai_out:  int   = 0
        self._start:    float = 0.0
        self._success:  bool  = True
        self._error:    Optional[str] = None

    def anthropic_tokens(self, input: int, output: int) -> None:
        self._ant_in  += input
        self._ant_out += output

    def openai_tokens(self, input: int, output: int) -> None:
        self._oai_in  += input
        self._oai_out += output

    def mark_error(self, message: str) -> None:
        self._success = False
        self._error   = message

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time.monotonic() - self._start
        if exc_type is not None:
            self._success = False
            self._error   = str(exc_val)

        # Estimate total cost across both providers
        cost = 0.0
        if self.proposer_model and self._ant_in + self._ant_out > 0:
            model = self.proposer_model if self.proposer_backend == "claude" else (
                self.challenger_model or ""
            )
            cost += _estimate_cost(model, self._ant_in, self._ant_out)
        if self.proposer_model and self._oai_in + self._oai_out > 0:
            model = self.proposer_model if self.proposer_backend == "openai" else (
                self.challenger_model or ""
            )
            cost += _estimate_cost(model, self._oai_in, self._oai_out)

        try:
            with _get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO usage_log (
                        timestamp, endpoint, caller_id, domain,
                        proposer_backend, proposer_model,
                        challenger_backend, challenger_model, rounds,
                        anthropic_input_tokens, anthropic_output_tokens,
                        openai_input_tokens, openai_output_tokens,
                        estimated_cost_usd, duration_seconds,
                        success, error_message
                    ) VALUES (
                        ?, ?, ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?
                    )
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        self.endpoint,
                        self.caller_id,
                        self.domain,
                        self.proposer_backend,
                        self.proposer_model,
                        self.challenger_backend,
                        self.challenger_model,
                        self.rounds,
                        self._ant_in,
                        self._ant_out,
                        self._oai_in,
                        self._oai_out,
                        round(cost, 6),
                        round(duration, 3),
                        1 if self._success else 0,
                        self._error,
                    ),
                )
                conn.commit()
        except Exception:
            # Never let logging failure break the main request
            pass

        return False  # don't suppress exceptions


# ── Usage summary query ───────────────────────────────────────────────────────

def usage_summary(days: int = 30) -> dict:
    """
    Return a summary of usage for the last N days.

    Returns a dict suitable for JSON serialization:
    {
        "period_days": 30,
        "total_calls": 142,
        "successful_calls": 139,
        "total_estimated_cost_usd": 1.23,
        "by_caller": {"user@example.com": {"calls": 45, "cost": 0.42}},
        "by_endpoint": {"/adversarial": {"calls": 20, "cost": 0.89}},
        "by_domain": {"engineering": {"calls": 12, "cost": 0.44}},
    }
    """
    try:
        conn = _get_conn()
    except Exception as exc:
        return {"error": str(exc)}

    cutoff = f"datetime('now', '-{days} days')"

    def q(sql: str) -> list[sqlite3.Row]:
        return conn.execute(sql).fetchall()

    totals = q(f"""
        SELECT COUNT(*) as calls,
               SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as ok,
               ROUND(SUM(estimated_cost_usd), 4) as cost
        FROM usage_log WHERE timestamp >= {cutoff}
    """)[0]

    by_caller = {
        row["caller_id"] or "anonymous": {
            "calls": row["calls"],
            "cost": round(row["cost"] or 0, 4),
        }
        for row in q(f"""
            SELECT caller_id, COUNT(*) as calls,
                   ROUND(SUM(estimated_cost_usd), 4) as cost
            FROM usage_log WHERE timestamp >= {cutoff}
            GROUP BY caller_id ORDER BY cost DESC
        """)
    }

    by_endpoint = {
        row["endpoint"]: {
            "calls": row["calls"],
            "cost": round(row["cost"] or 0, 4),
        }
        for row in q(f"""
            SELECT endpoint, COUNT(*) as calls,
                   ROUND(SUM(estimated_cost_usd), 4) as cost
            FROM usage_log WHERE timestamp >= {cutoff}
            GROUP BY endpoint ORDER BY calls DESC
        """)
    }

    by_domain = {
        row["domain"]: {
            "calls": row["calls"],
            "cost": round(row["cost"] or 0, 4),
        }
        for row in q(f"""
            SELECT domain, COUNT(*) as calls,
                   ROUND(SUM(estimated_cost_usd), 4) as cost
            FROM usage_log WHERE timestamp >= {cutoff} AND domain IS NOT NULL
            GROUP BY domain ORDER BY calls DESC
        """)
    }

    conn.close()

    return {
        "period_days":               days,
        "total_calls":               totals["calls"] or 0,
        "successful_calls":          totals["ok"]    or 0,
        "total_estimated_cost_usd":  round(totals["cost"] or 0, 4),
        "by_caller":                 by_caller,
        "by_endpoint":               by_endpoint,
        "by_domain":                 by_domain,
    }
