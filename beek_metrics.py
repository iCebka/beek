"""Non-decision-making instrumentation for Beek CEGIS campaigns.

The enumerator computes exact learning impact only for small, finite domains.
Its output is diagnostic: NEVER use its counts to select a synthesis candidate,
block a configuration, or change a learned constraint.
"""

from __future__ import annotations

from contextlib import contextmanager
from itertools import product
from math import prod
from time import perf_counter_ns
from typing import Any, Mapping


class RecordedConstraints(list):
    """A normal list of constraints with an out-of-band metrics snapshot.

    Remains compatible with callers expecting list and json.dumps(list).
    """

    def __init__(self, constraints=(), *, reasoning_metrics: dict | None = None):
        super().__init__(constraints)
        self.reasoning_metrics = reasoning_metrics or {}


def seconds(ns: int) -> float:
    return round(ns / 1_000_000_000, 9)


@contextmanager
def timed_stage(timings: dict[str, float], name: str, *, enabled: bool = True):
    """Measure wall-clock elapsed time, including failures, with a monotonic clock."""
    if not enabled:
        yield
        return
    start = perf_counter_ns()
    try:
        yield
    finally:
        timings[name] = round(timings.get(name, 0.0) + seconds(perf_counter_ns() - start), 9)


def _evaluate_expression(expression: dict, assignment: Mapping[str, int]) -> bool:
    """Evaluate Beek's existing, *bounded* declarative expression vocabulary."""
    if not isinstance(expression, dict):
        raise ValueError("Expression must be a dictionary")
    op = expression.get("op")
    if op in ("eq", "ne", "lt", "le", "gt", "ge"):
        variable = expression["parameter"]
        a, b = assignment[variable], expression["value"]
        return {
            "eq": lambda: a == b,
            "ne": lambda: a != b,
            "lt": lambda: a < b,
            "le": lambda: a <= b,
            "gt": lambda: a > b,
            "ge": lambda: a >= b,
        }[op]()
    if op in ("and", "or"):
        args = expression["args"]
        if not isinstance(args, list) or not args:
            raise ValueError(f"{op} must have nonempty args")
        values = [_evaluate_expression(arg, assignment) for arg in args]
        return all(values) if op == "and" else any(values)
    if op == "not":
        return not _evaluate_expression(expression["arg"], assignment)
    raise ValueError(f"Unsupported expression operator in instrumentation: {op!r}")


def count_learning_impact(
    parameters: Mapping[str, dict],
    blocked_candidates: list[dict],
    before_constraints: list[dict],
    after_constraints: list[dict],
    *,
    max_exact_space: int = 100_000,
) -> dict[str, Any]:
    """Exact, incremental impact given a FIXED set of blocked candidates.

    Count(BLOCKERS and BEFORE) - Count(BLOCKERS and AFTER) is the incremental
    number eliminated by learning; the just-failed candidate is already blocked.
    This is not a proof of learned-constraint soundness.
    """
    names = sorted(parameters)
    domains = [parameters[name]["domain"] for name in names]
    size = prod(len(domain) for domain in domains)
    result: dict[str, Any] = {
        "status": "EXACT" if size <= max_exact_space else "SKIPPED_LIMIT",
        "domain_size": size,
        "limit": max_exact_space,
        "blocked_candidates": len(blocked_candidates),
        "learned_constraints_before": len(before_constraints),
        "learned_constraints_after": len(after_constraints),
        "remaining_before": None,
        "remaining_after": None,
        "additional_candidates_eliminated": None,
        "remaining_after_blocking_only": None,
    }
    if size > max_exact_space:
        return result
    blocked = {tuple(candidate[name] for name in names) for candidate in blocked_candidates}
    before = after = unblocked = 0
    for values in product(*domains):
        if values in blocked:
            continue
        unblocked += 1
        candidate = dict(zip(names, values))
        if all(_evaluate_expression(c["expression"], candidate) for c in before_constraints):
            before += 1
        if all(_evaluate_expression(c["expression"], candidate) for c in after_constraints):
            after += 1
    result.update(
        remaining_before=before,
        remaining_after=after,
        additional_candidates_eliminated=before - after,
        remaining_after_blocking_only=unblocked,
    )
    return result


def aggregate_campaign_metrics(records: list[dict], total_wall_time_s: float) -> dict:
    """Aggregate exclusive stages once; do not add nested subprocess durations."""
    by_stage: dict[str, float] = {}
    process_by_stage: dict[str, float] = {}
    constraints_generated = constraints_accepted = extra_removed = z3_checks = 0
    rules_examined = rules_applicable = rules_executed = 0
    accurate_space_counts = 0
    for record in records:
        for name, value in record.get("timings_s", {}).items():
            by_stage[name] = round(by_stage.get(name, 0.0) + value, 9)
        for name, value in record.get("subprocess_wall_time_s_by_stage", {}).items():
            process_by_stage[name] = round(process_by_stage.get(name, 0.0) + value, 9)
        reasoning = record.get("reasoning", {})
        constraints_generated += reasoning.get("constraints_generated", 0)
        constraints_accepted += reasoning.get("constraints_accepted", 0)
        rules_examined += reasoning.get("rules_examined", 0)
        rules_applicable += reasoning.get("rules_applicable", 0)
        rules_executed += reasoning.get("rules_executed", 0)
        impact = record.get("search_space", {})
        if impact.get("status") == "EXACT":
            accurate_space_counts += 1
            extra_removed += impact.get("additional_candidates_eliminated", 0)
        z3_checks += record.get("z3", {}).get("check_calls", 0)
    return {
        "total_wall_time_s": round(total_wall_time_s, 9),
        "stage_wall_time_s": by_stage,
        "verification_wall_time_s": by_stage.get("verification_and_artifacts", 0.0),
        "reasoning_wall_time_s": by_stage.get("reasoning", 0.0),
        "synthesis_wall_time_s": by_stage.get("synthesis", 0.0),
        "subprocess_wall_time_s_by_stage": process_by_stage,
        "constraints_generated": constraints_generated,
        "constraints_accepted": constraints_accepted,
        "additional_candidates_eliminated": extra_removed if accurate_space_counts == len([
            r for r in records if r.get("reasoning", {}).get("constraints_generated", 0)
        ]) else None,
        "exact_search_space_measurements": accurate_space_counts,
        "rules_examined": rules_examined,
        "rules_applicable": rules_applicable,
        "rules_executed": rules_executed,
        "z3_check_count": z3_checks,
        "z3_solver_statistics_note": "Per-check native Z3 statistics are stored in each synthesis.json; not summed across calls",
        "initial_model_build_note": "The initial model was supplied to run_beek_cegis; initial model construction time is excluded",
    }
