"""Pluggable learning interface for Beek.

Engines:
  - 'vault': bounded, model-and-trace-conditioned reasoning rules (implemented)
  - 'llm': placeholder (prints selection, returns no learned constraints)
  - 'general': placeholder for future symbolic model analysis (same behavior)

The return format is the existing synthesis constraint protocol:
    [{"id": ..., "expression": {"op": ..., ...}, ...}, ...]

`run_beek_cegis` invokes a three-argument callback. Select the engine with:
    from functools import partial
    learn_constraints_fn=partial(learn_constraints, engine="vault")
"""

from __future__ import annotations

from beek_reasoning_rules import REASONING_RULES
from beek_metrics import RecordedConstraints, seconds
from time import perf_counter_ns

ENGINE_NAMES = ("vault", "llm", "general")


def learn_constraints(
    counterexample: dict,
    model: dict,
    parameters: dict,
    engine: str = "vault",
) -> list[dict]:
    """Return justified constraints, or [] if no supported rule applies.

    Non-vault engines intentionally do nothing at this stage. Invalid engine
    names are rejected rather than silently changing synthesis behavior.
    """
    started_ns = perf_counter_ns()
    if engine not in ENGINE_NAMES:
        raise ValueError(f"Unsupported reasoning engine {engine!r}; select one of {ENGINE_NAMES}")

    report = {
        "engine": engine,
        "rules_examined": 0,
        "rules_applicable": 0,
        "rules_executed": 0,
        "constraints_generated": 0,
        "rules": [],
    }
    if engine != "vault":
        print(f"[Beek reasoning] Selected engine: {engine} (not implemented; returning [])")
        report["total_wall_time_s"] = seconds(perf_counter_ns() - started_ns)
        return RecordedConstraints([], reasoning_metrics=report)

    print("[Beek reasoning] Selected engine: vault")
    if not isinstance(counterexample, dict) or not isinstance(parameters, dict) or not isinstance(model, dict):
        raise TypeError("counterexample, model and parameters must be dictionaries")
    violated = counterexample.get("violated_intents", [])
    if not isinstance(violated, list):
        raise ValueError("counterexample.violated_intents must be a list")

    learned = []
    for rule_id, rule in REASONING_RULES.items():
        start_ns = perf_counter_ns()
        rule_report = {
            "rule_id": rule_id,
            "status": "SKIPPED",
            "reason": None,
            "applicable": False,
            "constraints_generated": 0,
        }
        report["rules_examined"] += 1
        try:
            if not set(violated).intersection(rule["applicable_intents"]):
                rule_report["reason"] = "intent_mismatch"
                continue
            if not set(rule["required_parameters"]).issubset(parameters):
                rule_report["reason"] = "missing_required_parameters"
                continue
            if not rule["applicability"](counterexample, model, parameters):
                rule_report["reason"] = "applicability_rejected"
                continue
            rule_report["applicable"] = True
            report["rules_applicable"] += 1
            report["rules_executed"] += 1
            rule_report["status"] = "EXECUTED"
            produced = rule["handler"](counterexample, model, parameters)
            if not isinstance(produced, list):
                raise TypeError(f"Reasoning handler {rule_id} did not return a list")
            for constraint in produced:
                if not isinstance(constraint, dict) or not isinstance(constraint.get("expression"), dict):
                    raise ValueError(f"Invalid learned constraint from {rule_id}")
                learned.append(constraint)
            rule_report["constraints_generated"] = len(produced)
            print(f"[Beek reasoning] Rule {rule_id}: {len(produced)} constraint(s)")
        except Exception as exc:
            rule_report["status"] = "ERROR"
            rule_report["reason"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            rule_report["wall_time_s"] = seconds(perf_counter_ns() - start_ns)
            report["rules"].append(rule_report)
    if not learned:
        print("[Beek reasoning] No applicable vault rule; falling back to candidate blocking")
    report["constraints_generated"] = len(learned)
    report["total_wall_time_s"] = seconds(perf_counter_ns() - started_ns)
    return RecordedConstraints(learned, reasoning_metrics=report)
