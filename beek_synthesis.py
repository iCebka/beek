import re
import json
from z3 import *
from time import perf_counter_ns
from beek_metrics import seconds
from copy import deepcopy
import yaml
import os
from pathlib import Path
import sys
import inspect

def read_raw_trace(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def parse_counterexample(raw_trace):
    intent_match = re.search(
        r"(?m)^\s*Violating intent\(s\):\s*(.+)$",
        raw_trace
    )

    if intent_match is None:
        raise ValueError("No violating intents found")

    violated_intents = intent_match.group(1).split()

    if "Counterexample:" not in raw_trace:
        raise ValueError("No counterexample found")

    section = raw_trace.split("Counterexample:", 1)[1]
    section = section.split("Summary:", 1)[0]

    pattern = re.compile(
        r"(?m)^\[(\d+)\]\[([^]\n]+)\]\s*(.*)$"
    )

    steps = []

    for match in pattern.finditer(section):
        steps.append({
            "index": int(match.group(1)),
            "component": match.group(2),
            "description": match.group(3),
        })

    if not steps:
        raise ValueError("No counterexample steps found")

    return {
        "violated_intents": violated_intents,
        "steps": steps,
    }


def build_repair_problem(
    counterexample,
    parameters,
    constraints=None,
    blocked_candidates=None,
    objective="min_changes_then_distance"
):
    if not parameters:
        raise ValueError("No repair parameters defined")

    constraints = list(constraints or [])
    names = set(parameters)

    # Only a configuration that has ACTUALLY FAILED verification is blocked.
    if not counterexample.get("violated_intents"):
        raise ValueError("A confirmed counterexample is required to block a candidate")

    current_candidate = {}
    for name, info in parameters.items():
        if "original" not in info:
            raise ValueError(f"Missing original value for {name}")
        # Block the configuration that Kivi has JUST refuted, not the baseline.
        # Legacy single-iteration callers may omit "current".
        value = info.get("current", info["original"])
        if type(value) is not int or value not in info["domain"]:
            raise ValueError(f"Current value outside domain for {name}: {value}")
        current_candidate[name] = value

    blocked = []
    for candidate in blocked_candidates or []:
        if set(candidate) != names:
            raise ValueError("Each blocked candidate must assign every repair parameter")
        for name, value in candidate.items():
            if type(value) is not int or value not in parameters[name]["domain"]:
                raise ValueError(f"Invalid blocked value: {name}={value}")
        if candidate not in blocked:
            blocked.append(dict(candidate))

    # The current candidate is the one proved faulty by this counterexample.
    if current_candidate not in blocked:
        blocked.append(current_candidate)

    return {
        "parameters": parameters,
        "constraints": constraints,
        "blocked_candidates": blocked,
        "objective": objective,
    }

def encode_expression(expr, symbols):
    op = expr["op"]

    comparisons = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "lt": lambda a, b: a < b,
        "le": lambda a, b: a <= b,
        "gt": lambda a, b: a > b,
        "ge": lambda a, b: a >= b,
    }

    if op in comparisons:
        parameter = expr["parameter"]
        value = expr["value"]

        return comparisons[op](symbols[parameter], value)

    if op == "and":
        args = expr["args"]
        if not args:
            raise ValueError("AND requires at least one argument")
        return And(*[
            encode_expression(arg, symbols)
            for arg in args
        ])

    if op == "or":
        args = expr["args"]
        if not args:
            raise ValueError("OR requires at least one argument")
        return Or(*[
            encode_expression(arg, symbols)
            for arg in args
        ])

    if op == "not":
        return Not(
            encode_expression(expr["arg"], symbols)
        )

    raise ValueError(f"Unsupported operator: {op}")

def synthesize_repair(problem):
    synthesis_start_ns = perf_counter_ns()
    parameters = problem["parameters"]
    names = sorted(parameters)

    if not names:
        raise ValueError("No modifiable parameters defined")

    symbols = {
        name: Int(name)
        for name in names
    }

    optimizer = Optimize()
    optimizer.set(priority="lex")

    # 1. Parameter domains
    for name in names:
        domain = parameters[name]["domain"]

        if not domain:
            raise ValueError(f"Empty domain for {name}")

        optimizer.add(
            Or(*[
                symbols[name] == value
                for value in domain
            ])
        )

    # 2. Learned constraints
    for constraint in problem["constraints"]:
        expression = encode_expression(
            constraint["expression"],
            symbols
        )

        optimizer.add(expression)

    # 3. Previously rejected candidates
    for blocked in problem["blocked_candidates"]:
        if set(blocked) != set(names):
            raise ValueError(
                "Blocked candidate must assign every parameter"
            )

        optimizer.add(
            Or(*[
                symbols[name] != blocked[name]
                for name in names
            ])
        )

    # 4. Optimization
    objective = problem.get("objective", "none")

    if objective == "min_changes_then_distance":
        changes = Sum(*[
            If(
                symbols[name] != parameters[name]["original"],
                1,
                0
            )
            for name in names
        ])

        distance = Sum(*[
            Abs(
                symbols[name] - parameters[name]["original"]
            )
            for name in names
        ])

        optimizer.minimize(changes)
        optimizer.minimize(distance)

        # Deterministic tie breaking
        for name in names:
            optimizer.minimize(symbols[name])

    elif objective != "none":
        raise ValueError(f"Unsupported objective: {objective}")

    # 5. Solve (retain the exact Z3 problem, optimization, and order).
    build_time_s = seconds(perf_counter_ns() - synthesis_start_ns)
    check_start_ns = perf_counter_ns()
    status = optimizer.check()
    check_time_s = seconds(perf_counter_ns() - check_start_ns)
    try:
        native_stats = optimizer.statistics()
        z3_statistics = {}
        for key in native_stats.keys():
            value = native_stats.get_key_value(key)
            z3_statistics[key] = value if type(value) in (int, float, bool, str) else str(value)
    except Exception as exc:
        z3_statistics = {"collection_error": f"{type(exc).__name__}: {exc}"}

    def metrics():
        return {
            "build_wall_time_s": build_time_s,
            "check_wall_time_s": check_time_s,
            "total_wall_time_s": seconds(perf_counter_ns() - synthesis_start_ns),
            "check_calls": 1,
            "variables": len(names),
            "domain_assertions": len(names),
            "learned_assertions": len(problem["constraints"]),
            "blocked_assertions": len(problem["blocked_candidates"]),
            "optimization_objectives": (2 + len(names)) if objective == "min_changes_then_distance" else 0,
            "objective": objective,
            "z3_statistics": z3_statistics,
        }


    if status == sat:
        model = optimizer.model()

        candidate = {
            name: model.eval(symbols[name]).as_long()
            for name in names
        }

        return {
            "status": "SAT",
            "candidate": candidate,
            "verification_status": "NOT_RUN",
            "metrics": metrics(),
        }

    if status == unsat:
        return {
            "status": "UNSAT",
            "candidate": None,
            "verification_status": "NOT_RUN",
            "metrics": metrics(),
        }

    return {
        "status": "UNKNOWN",
        "candidate": None,
        "verification_status": "NOT_RUN",
        "reason": optimizer.reason_unknown(),
        "metrics": metrics(),
    }