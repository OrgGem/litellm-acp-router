"""Native observations and conservative, coverage-aware projections."""

import json
import sqlite3
import time
from dataclasses import dataclass

from .contract import canonical, validate
from .errors import ProtocolError

COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


@dataclass(frozen=True)
class Projection:
    counters: dict
    completeness: str
    coverage: frozenset

    def openai(self):
        if self.completeness != "complete":
            return None
        i, o = self.counters["input_tokens"], self.counters["output_tokens"]
        result = {"prompt_tokens": i, "completion_tokens": o, "total_tokens": i + o}
        cached = self.counters["cache_read_tokens"]
        reasoning = self.counters["reasoning_tokens"]
        if cached is not None:
            result["prompt_tokens_details"] = {"cached_tokens": cached}
        if reasoning is not None:
            result["completion_tokens_details"] = {"reasoning_tokens": reasoning}
        return result


class Ledger:
    """In-memory per-invocation counters. Optional SQLite stores metadata only.

    Never allocate a native-turn snapshot to an HTTP round without explicit step
    ownership. Store failures degrade accounting without affecting inference.
    """

    def __init__(
        self,
        path=None,
        *,
        max_invocations=256,
        max_observations=8192,
        max_invocation_observations=128,
        max_coverage=128,
        retention_seconds=30 * 86400,
    ):
        if (
            type(max_invocations) is not int
            or max_invocations < 1
            or type(max_observations) is not int
            or max_observations < 1
            or type(max_invocation_observations) is not int
            or max_invocation_observations < 1
            or type(max_coverage) is not int
            or max_coverage < 1
            or retention_seconds <= 0
        ):
            raise ValueError("Invalid usage retention limits")
        self.events = {}
        self.current = {}
        self.bindings = {}
        self.invocations = {}
        self.archived = {}
        self.overflow = set()
        self.max_invocations = max_invocations
        self.max_observations = max_observations
        self.max_invocation_observations = max_invocation_observations
        self.max_coverage = max_coverage
        self.retention_seconds = retention_seconds
        self.degraded = False
        self.db = None
        if path is not None:
            try:
                self.db = sqlite3.connect(path, timeout=0.05)
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS native_usage_v2 (invocation TEXT, epoch TEXT, event TEXT, observation TEXT NOT NULL, PRIMARY KEY(invocation,epoch,event))"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS native_summaries_v2 (invocation TEXT PRIMARY KEY, updated REAL NOT NULL, summary TEXT NOT NULL)"
                )
                self.db.commit()
                rows = self.db.execute(
                    "SELECT invocation, summary FROM native_summaries_v2 WHERE updated >= ? ORDER BY updated DESC LIMIT ?",
                    (time.time() - self.retention_seconds, max_invocations),
                ).fetchall()
                for invocation, summary in rows:
                    value = json.loads(summary)
                    if self._safe_summary(value):
                        if value["status"] == "running":
                            value["status"] = "interrupted"
                            value["usage_completeness"] = (
                                "partial"
                                if value["input_tokens"] is not None
                                or value["output_tokens"] is not None
                                else "unknown"
                            )
                        self.archived[invocation] = value
                self._prune_database()
            except (sqlite3.Error, ValueError, TypeError):
                self.degraded = True

    @staticmethod
    def _safe_summary(value):
        allowed = {
            "invocation_id",
            "key_scope",
            "model",
            "backend",
            "status",
            "usage_completeness",
            "source",
            "request_count",
            "updated_at",
            *COUNTERS,
        }
        return (
            isinstance(value, dict)
            and set(value) == allowed
            and all(
                value[k] is None or type(value[k]) is int and value[k] >= 0
                for k in COUNTERS
            )
            and value["status"]
            in {"running", "succeeded", "failed", "cancelled", "interrupted"}
            and value["usage_completeness"] in {"complete", "partial", "unknown"}
        )

    def begin(
        self, invocation, *, key_scope=None, model=None, backend=None, request_id=None
    ):
        self._prune(reserve=1)
        if len(self.invocations) + len(self.archived) >= self.max_invocations:
            self.degraded = True
            return False
        self.invocations[invocation] = {
            "key_scope": key_scope,
            "model": model,
            "backend": backend,
            "status": "running",
            "requests": {request_id} if request_id else set(),
            "updated_at": time.time(),
            "terminal_completeness": None,
        }
        self.archived.pop(invocation, None)
        return True

    def next_request(self, invocation, request_id):
        if invocation in self.invocations:
            self.invocations[invocation]["requests"].add(request_id)

    def finish(self, invocation, status, *, completeness=None):
        if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise ValueError("Invalid native outcome")
        if completeness not in {None, "complete", "partial", "unknown"}:
            raise ValueError("Invalid terminal completeness")
        if invocation in self.invocations:
            self.invocations[invocation].update(
                status=status,
                updated_at=time.time(),
                terminal_completeness=completeness,
            )
            self._persist_summary(invocation)
        self._prune()

    def _entry(self, invocation):
        if invocation in self.archived:
            return dict(self.archived[invocation])
        metadata = self.invocations[invocation]
        projection = self.project(invocation)
        completeness = projection.completeness
        if metadata["status"] != "succeeded" and completeness == "complete":
            completeness = "partial"
        terminal = metadata["terminal_completeness"]
        if terminal in {"partial", "unknown"} and completeness == "complete":
            completeness = "partial"
        sources = sorted(
            {
                o["source"]
                for o in self.current.values()
                if o["invocation_id"] == invocation
            }
        )
        return {
            "invocation_id": invocation,
            **{
                key: metadata[key]
                for key in ("key_scope", "model", "backend", "status", "updated_at")
            },
            "usage_completeness": completeness,
            "source": sources,
            **projection.counters,
            "request_count": len(metadata["requests"]),
        }

    def status_snapshot(self):
        self._prune()
        rows = [self._entry(key) for key in {*self.invocations, *self.archived}]
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        totals = {
            name: sum(row[name] for row in rows if row[name] is not None)
            if any(row[name] is not None for row in rows)
            else None
            for name in COUNTERS
        }
        totals.update(
            {
                kind: sum(row["usage_completeness"] == kind for row in rows)
                for kind in ("complete", "partial", "unknown")
            }
        )
        return {
            "available": True,
            "degraded": self.degraded,
            "persistence": "sqlite" if self.db is not None else "memory",
            "invocations": rows,
            "totals": totals,
        }

    def _remove(self, invocation):
        self.invocations.pop(invocation, None)
        self.archived.pop(invocation, None)
        self.overflow.discard(invocation)
        self.events = {
            key: value for key, value in self.events.items() if key[0] != invocation
        }
        self.current = {
            key: value for key, value in self.current.items() if key[0] != invocation
        }
        self.bindings = {
            key: value for key, value in self.bindings.items() if key[0] != invocation
        }

    def _prune(self, reserve=0):
        rows = [
            (key, metadata["updated_at"])
            for key, metadata in self.invocations.items()
            if metadata["status"] != "running"
        ] + [(key, row["updated_at"]) for key, row in self.archived.items()]
        for key, updated in sorted(rows, key=lambda row: row[1]):
            remove_metadata = (
                updated < time.time() - self.retention_seconds
                or len(self.invocations) + len(self.archived) + reserve
                > self.max_invocations
            )
            if remove_metadata:
                self._remove(key)
            elif len(self.events) >= self.max_observations and key in self.invocations:
                summary = self._entry(key)
                self._remove(key)
                self.archived[key] = summary

    def _persist_summary(self, invocation, observation=None):
        if self.db is None:
            return
        try:
            row = self._entry(invocation)
            # A native observation and its retained summary are one atomic unit.
            # This prevents unowned raw rows after a failed write or crash.
            if observation is not None:
                self._persist(observation)
            self.db.execute(
                "INSERT OR REPLACE INTO native_summaries_v2 VALUES (?,?,?)",
                (invocation, row["updated_at"], canonical(row).decode()),
            )
            self.db.commit()
            self._prune_database()
        except sqlite3.Error:
            self.db.rollback()
            self.degraded = True

    def _prune_database(self):
        if self.db is None:
            return
        try:
            # Only rows owned by this additive schema are subject to retention;
            # pre-existing observations lacking a summary remain untouched.
            old = [
                row[0]
                for row in self.db.execute(
                    "SELECT invocation FROM native_summaries_v2 WHERE updated < ? OR invocation NOT IN (SELECT invocation FROM native_summaries_v2 ORDER BY updated DESC LIMIT ?)",
                    (time.time() - self.retention_seconds, self.max_invocations),
                )
            ]
            for invocation in old:
                self.db.execute(
                    "DELETE FROM native_usage_v2 WHERE invocation=?", (invocation,)
                )
                self.db.execute(
                    "DELETE FROM native_summaries_v2 WHERE invocation=?", (invocation,)
                )
            self.db.execute(
                "DELETE FROM native_usage_v2 WHERE invocation IN (SELECT invocation FROM native_summaries_v2) AND rowid NOT IN (SELECT rowid FROM native_usage_v2 WHERE invocation IN (SELECT invocation FROM native_summaries_v2) ORDER BY rowid DESC LIMIT ?)",
                (self.max_observations,),
            )
            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            self.degraded = True

    def bind_step(self, invocation, step, request_id):
        key = invocation, step
        if key in self.bindings and self.bindings[key] != request_id:
            raise ProtocolError("ambiguous_usage_owner", 502)
        self.bindings[key] = request_id

    def observe(self, observation):
        validate("UsageObservation", observation)
        o = json.loads(canonical(observation))
        if o["completeness"] == "complete" and any(
            o[k] is None for k in ("input_tokens", "output_tokens")
        ):
            raise ProtocolError("invalid_usage_completeness", 502)
        if (
            o["cache_read_tokens"] is not None
            and o["input_tokens"] is not None
            and o["cache_read_tokens"] > o["input_tokens"]
        ):
            raise ProtocolError("invalid_usage_breakdown", 502)
        if (
            o["reasoning_tokens"] is not None
            and o["output_tokens"] is not None
            and o["reasoning_tokens"] > o["output_tokens"]
        ):
            raise ProtocolError("invalid_usage_breakdown", 502)
        event = o["invocation_id"], o["epoch"], o["event_id"]
        if event in self.events:
            if self.events[event] != o:
                raise ProtocolError("contradictory_usage_event", 502)
            return False
        if o["invocation_id"] not in self.invocations:
            if not self.begin(o["invocation_id"]):
                return False
        self._prune()
        if (
            len(self.events) >= self.max_observations
            or sum(key[0] == o["invocation_id"] for key in self.events)
            >= self.max_invocation_observations
            or len(o["coverage"] or []) > self.max_coverage
        ):
            self.degraded = True
            self.overflow.add(o["invocation_id"])
            self._persist_summary(o["invocation_id"])
            return False
        key = (
            o["invocation_id"],
            o["epoch"],
            o["scope"],
            o["step_id"],
            tuple(sorted(o["coverage"] or [])),
        )
        old = self.current.get(key)
        if old is not None and old["semantics"] != o["semantics"]:
            raise ProtocolError("ambiguous_usage_semantics", 502)
        if (
            old is not None
            and old["semantics"] == "snapshot"
            and old["sequence"] == o["sequence"]
            and {k: v for k, v in old.items() if k != "event_id"}
            != {k: v for k, v in o.items() if k != "event_id"}
        ):
            raise ProtocolError("contradictory_usage_sequence", 502)
        if old is None or o["sequence"] > old["sequence"]:
            value = dict(o)
            if old is not None and o["semantics"] == "delta":
                for counter in COUNTERS:
                    value[counter] = (
                        None
                        if old[counter] is None or o[counter] is None
                        else old[counter] + o[counter]
                    )
                if old["completeness"] != "complete":
                    value["completeness"] = "partial"
            self.current[key] = value
        elif o["semantics"] == "delta":
            # Deltas cannot be safely dropped as stale snapshots can.
            raise ProtocolError("unordered_usage_delta", 502)
        self.events[event] = o
        self.invocations[o["invocation_id"]]["updated_at"] = time.time()
        self._persist_summary(o["invocation_id"], observation=o)
        return True

    def _persist(self, observation):
        self.db.execute(
            "INSERT OR IGNORE INTO native_usage_v2 VALUES (?,?,?,?)",
            (
                observation["invocation_id"],
                observation["epoch"],
                observation["event_id"],
                canonical(observation).decode(),
            ),
        )

    def project(self, invocation, *, request_id=None):
        unknown = Projection(dict.fromkeys(COUNTERS), "unknown", frozenset())
        if invocation in self.archived:
            if request_id is not None:
                return unknown
            row = self.archived[invocation]
            return Projection(
                {name: row[name] for name in COUNTERS},
                row["usage_completeness"],
                frozenset(),
            )
        values = [o for o in self.current.values() if o["invocation_id"] == invocation]
        if not values or len({o["epoch"] for o in values}) != 1:
            return unknown
        steps = {
            tuple(o["coverage"]): o
            for o in values
            if o["scope"] == "step"
            and o["coverage"] == [o["step_id"]]
            and o["final"]
            and o["completeness"] == "complete"
        }
        for aggregate in values:
            coverage = set(aggregate["coverage"] or [])
            if (
                aggregate["scope"] == "step"
                or not coverage
                or not aggregate["final"]
                or aggregate["completeness"] != "complete"
            ):
                continue
            components = [steps.get((step,)) for step in coverage]
            if all(component is not None for component in components):
                for counter in COUNTERS:
                    if aggregate[counter] is not None and all(
                        component[counter] is not None for component in components
                    ):
                        if (
                            sum(component[counter] for component in components)
                            != aggregate[counter]
                        ):
                            return unknown
        for a in values:
            for b in values:
                if a is b or not a["coverage"] or not b["coverage"]:
                    continue
                ca, cb = set(a["coverage"]), set(b["coverage"])
                if (
                    ca <= cb
                    and a["completeness"] == b["completeness"] == "complete"
                    and a["final"]
                    and b["final"]
                ):
                    for counter in COUNTERS:
                        x, y = a[counter], b[counter]
                        if (
                            x is not None
                            and y is not None
                            and (x > y or (ca == cb and x != y))
                        ):
                            return unknown
        if request_id is not None:
            owned = {
                step
                for (inv, step), req in self.bindings.items()
                if inv == invocation and req == request_id
            }
            values = [
                o for o in values if o["coverage"] and set(o["coverage"]) <= owned
            ]
            if not values:
                return unknown
        # Prefer one covering snapshot to its component steps, never add both.
        values.sort(
            key=lambda o: (len(o["coverage"] or []), o["sequence"]), reverse=True
        )
        covered, selected = set(), []
        incomplete = False
        for value in values:
            coverage = set(value["coverage"] or [])
            if not coverage:
                incomplete = True
                continue
            overlap = covered & coverage
            if overlap:
                if coverage <= covered:
                    continue
                # Partly overlapping aggregates cannot be subtracted reliably.
                incomplete = True
                continue
            selected.append(value)
            covered |= coverage
        if not selected:
            return unknown
        counters = {
            k: None
            if any(v[k] is None for v in selected)
            else sum(v[k] for v in selected)
            for k in COUNTERS
        }
        complete = (
            not incomplete
            and invocation not in self.overflow
            and all(v["completeness"] == "complete" and v["final"] for v in selected)
            and counters["input_tokens"] is not None
            and counters["output_tokens"] is not None
        )
        return Projection(
            counters, "complete" if complete else "partial", frozenset(covered)
        )

    def close(self):
        if self.db is not None:
            self.db.close()
