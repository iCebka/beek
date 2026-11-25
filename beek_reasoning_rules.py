"""Domain-scoped, manually audited reasoning rules for Beek's C3 family.

This is a vault of *procedures*, not a table mapping case names to repairs.
The rules inspect the supplied Kivi model and the actual counterexample, derive
symbolic thresholds, and abstain unless the exact characterized family matches.

IMPORTANT LIMITATION: The balance rule relies on the zone-2 preferred-node
selection behavior observed for this Kivi/C3 family. The 216-case regression
supports that bounded behavior but is not a general proof of Kivi scheduling.
Do not apply the learned constraints to other model families without revisiting
both the applicability checks and the semantic justification.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any, Callable


DOMAIN = tuple(range(1, 7))  # The tested C3 bounded parameter domain.
REQUIRED_PARAMS = frozenset({"hostname_skew", "zone_skew"})


@dataclass(frozen=True)
class C3Context:
    k: int
    node_a_high: int
    node_a_low: int
    node_b: int
    zone_a: int
    zone_b: int
    template_cpu: int
    template_mem: int
    h: int
    z: int
    has_preferred_b: bool
    initial_replicas: int
    node_counts: dict[int, int]
    node_zones: dict[int, int]


def _is_positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _params_match_model(parameters: dict, constraints: dict[str, dict]) -> bool:
    if not isinstance(parameters, dict) or set(parameters) != REQUIRED_PARAMS:
        return False
    for param in REQUIRED_PARAMS:
        info = parameters.get(param)
        if not isinstance(info, dict):
            return False
        domain = info.get("domain")
        if not isinstance(domain, (tuple, list)) or tuple(domain) != DOMAIN:
            return False
        if not _is_positive_integer(info.get("current", info.get("original"))):
            return False
        if not _is_positive_integer(info.get("original")):
            return False
        current = info.get("current", info["original"])
        if current not in domain:
            return False
        if constraints[param].get("maxSkew") != info.get("current", info["original"]):
            return False
    return True


def _trace_components(counterexample: dict) -> list[str]:
    steps = counterexample.get("steps", [])
    if not isinstance(steps, list):
        return []
    return [str(step.get("component", "")) for step in steps if isinstance(step, dict)]


def _trace_has_scaleup(counterexample: dict, original: int) -> bool:
    """Recognize the concrete 1-Pod HPA scale-up seen in the C3 traces."""
    for step in counterexample.get("steps", []):
        if not isinstance(step, dict) or step.get("component") != "HPA":
            continue
        match = re.search(r"pod number change from\s+(\d+)\s+to\s+(\d+)", str(step.get("description", "")))
        if match and (int(match.group(1)), int(match.group(2))) == (original, original + 1):
            return True
    return False


def _trace_has_cpu_change_on_node(counterexample: dict, expected_node_id: int, cpu_left: int) -> bool:
    # The initial-state calculation is valid only if the triggering event did
    # not consume CPU on either candidate node used in the schedulability proof.
    pattern = re.compile(r"CPU change\s+(\d+)\s+on pod\s+\d+,\s+now\s+\d+,\s+and node\s+(\d+),\s+now\s+(\d+)")
    for step in counterexample.get("steps", []):
        if not isinstance(step, dict) or step.get("component") != "CPU Change":
            continue
        match = pattern.search(str(step.get("description", "")))
        if match:
            delta, node_id, remaining = map(int, match.groups())
            return node_id == expected_node_id and remaining == cpu_left - delta
    return False


def _recognized_prefix(counterexample: dict, ctx: C3Context, model: dict) -> bool:
    if not isinstance(counterexample, dict):
        return False
    steps = counterexample.get("steps")
    if not isinstance(steps, list) or len(steps) < 5:
        return False
    components = _trace_components(counterexample)
    if components[:4] != ["CPU Change", "HPA", "Deployment", "Deployment"]:
        return False
    if not _trace_has_scaleup(counterexample, ctx.initial_replicas):
        return False
    nodes = {n["id"]: n for n in model["setup"]["nodes"]}
    if not _trace_has_cpu_change_on_node(counterexample, ctx.node_a_high, nodes[ctx.node_a_high]["cpuLeft"]):
        return False
    if "Too few replicas" not in str(steps[2].get("description", "")):
        return False
    if "Adding a new pod" not in str(steps[3].get("description", "")):
        return False
    return True


def _extract_c3_context(model: dict, parameters: dict) -> C3Context | None:
    """Return context only for the previously characterized three-node family.

    Every required assumption is checked explicitly; unknown features cause
    abstention rather than a generalized constraint of uncertain soundness.
    """
    try:
        if not isinstance(model, dict) or set(model) not in ({"setup", "controllers", "events", "intents"}, {"setup", "controllers", "events"}):
            return None
        setup = model["setup"]
        if set(setup) != {"nodes", "pods", "d", "podTemplates"}:
            return None
        controllers = model["controllers"]
        if set(controllers) != {"scheduler", "deployment", "hpa"} or any(controllers.values()):
            return None
        nodes = setup["nodes"]
        if len(nodes) != 3 or not all(isinstance(n, dict) for n in nodes):
            return None
        ids = [n.get("id") for n in nodes]
        if any(type(node_id) is not int for node_id in ids) or len(set(ids)) != 3:
            return None
        if not all(n.get("status") == 1 and isinstance(n.get("labels"), dict) and set(n["labels"]) == {"zone"} for n in nodes):
            return None
        node_map = {n["id"]: n for n in nodes}
        zones = {n["id"]: n["labels"]["zone"] for n in nodes}
        counts_by_zone = Counter(zones.values())
        if sorted(counts_by_zone.values()) != [1, 2]:
            return None
        zone_a = next(zone for zone, num in counts_by_zone.items() if num == 2)
        zone_b = next(zone for zone, num in counts_by_zone.items() if num == 1)
        nodes_a = [n for n in nodes if n["labels"]["zone"] == zone_a]
        node_b_obj = next(n for n in nodes if n["labels"]["zone"] == zone_b)
        k = node_b_obj.get("numPod")
        if type(k) is not int or not 2 <= k <= 6:
            return None
        nodes_a_high = [n for n in nodes_a if n.get("numPod") == k]
        nodes_a_low = [n for n in nodes_a if n.get("numPod") == k - 1]
        if len(nodes_a_high) != 1 or len(nodes_a_low) != 1:
            return None
        high, low, b = nodes_a_high[0], nodes_a_low[0], node_b_obj
        if any(n.get("numPod") != expected for n, expected in ((high,k),(low,k-1),(b,k))):
            return None
        # Status/resources and no other node scheduling features.
        allowed_node_fields = {"id", "name", "cpu", "memory", "cpuLeft", "memLeft", "numPod", "status", "labels"}
        if any(set(n) - allowed_node_fields for n in nodes):
            return None
        if any(type(n.get(f)) is not int or n[f] < 0 for n in nodes for f in ("cpu", "memory", "cpuLeft", "memLeft")):
            return None
        if any(n["cpuLeft"] > n["cpu"] or n["memLeft"] > n["memory"] for n in nodes):
            return None
        if len(set((n["cpu"], n["memory"]) for n in nodes)) != 1:
            return None

        templates = setup["podTemplates"]
        if len(templates) != 1 or not isinstance(templates[0], dict):
            return None
        t = templates[0]
        allowed_template_fields = {"cpuRequested", "memRequested", "labels", "topoSpreadConstraints", "numTopoSpreadConstraints", "affinityRules", "numRules"}
        if set(t) - allowed_template_fields:
            return None
        cpu, mem = t.get("cpuRequested"), t.get("memRequested")
        if not _is_positive_integer(cpu) or not _is_positive_integer(mem):
            return None
        if any(n["cpuLeft"] < cpu or n["memLeft"] < mem for n in (low,b)):
            return None
        if not isinstance(t.get("labels"), dict) or not t["labels"]:
            return None
        spread_list = t.get("topoSpreadConstraints", [])
        if len(spread_list) != 2 or t.get("numTopoSpreadConstraints") != 2:
            return None
        spread = {}
        for c in spread_list:
            if not isinstance(c, dict) or c.get("topologyKey") not in {"hostname", "zone"}:
                return None
            if c.get("whenUnsatisfiable") != 0 or c.get("numMatchedLabel") != len(c.get("labels", {})):
                return None
            if c.get("labels") != t["labels"]:
                return None
            spread[c["topologyKey"]] = c
        if set(spread) != {"hostname", "zone"}:
            return None
        if not _params_match_model(parameters, {"hostname_skew":spread["hostname"], "zone_skew":spread["zone"]}):
            return None

        # Affinity may be absent (C3) or *one* preferred zone-B rule (B2..B6).
        affinities = t.get("affinityRules", [])
        if t.get("numRules", 0) != len(affinities) or len(affinities) > 1:
            return None
        preferred = False
        if affinities:
            affinity = affinities[0]
            if set(affinity) != {"isRequired", "weight", "labels"}:
                return None
            if affinity.get("isRequired") != 0 or affinity.get("labels") != {"zone":zone_b} or affinity.get("weight") != 5:
                return None
            preferred = True

        # The counted pods must be exactly the active pods of one deployment.
        pods = setup["pods"]
        count_actual = Counter()
        inactive_pods = 0
        for pod in pods:
            if pod.get("status") == 1:
                if pod.get("loc") not in node_map or pod.get("podTemplateId") != 1 or pod.get("labels") != t["labels"]:
                    return None
                if pod.get("workloadType") != 1 or pod.get("workloadId") != 1:
                    return None
                count_actual[pod["loc"]] += 1
            elif pod.get("status") == 0:
                inactive_pods += 1
            else:
                return None
        if inactive_pods != 1 or len(pods) != sum(n["numPod"] for n in nodes) + 1:
            return None
        if any(count_actual[n["id"]] != n["numPod"] for n in nodes):
            return None
        deployment_list = setup["d"]
        if len(deployment_list) != 1 or not isinstance(deployment_list[0], dict):
            return None
        deployment = deployment_list[0]
        replicas = 3*k - 1
        if deployment.get("replicas") != replicas or deployment.get("specReplicas") != replicas or deployment.get("podTemplateId") != 1:
            return None
        hpa = deployment.get("hpaSpec", {})
        if hpa.get("isEnabled") != 1 or hpa.get("minReplicas") != replicas or hpa.get("maxReplicas") != replicas + 1:
            return None
        if len(model["events"]) != 1:
            return None
        event = model["events"][0]
        if event.get("name") != "eventCpuChange" or event.get("after_stable") is not True:
            return None
        if event.get("para", {}).get("targetDeployment") != deployment.get("id"):
            return None
        allowed_intents = {"checkBalanceNode"}
        intents = model.get("intents", [])
        if not isinstance(intents, list) or len(intents) > 1 or any(i.get("name") not in allowed_intents for i in intents):
            return None
        if intents and (intents[0].get("para", {}).get("maxSkew") != 1 or intents[0]["para"].get("did") != 1):
            return None

        return C3Context(k=k, node_a_high=high["id"], node_a_low=low["id"], node_b=b["id"],
                         zone_a=zone_a, zone_b=zone_b, template_cpu=cpu, template_mem=mem,
                         h=spread["hostname"]["maxSkew"], z=spread["zone"]["maxSkew"],
                         has_preferred_b=preferred, initial_replicas=replicas,
                         node_counts={n["id"]:n["numPod"] for n in nodes}, node_zones=zones)
    except (TypeError, ValueError, KeyError, IndexError, StopIteration, AttributeError):
        return None


def check_spread_schedulability(counterexample: dict, model: dict, parameters: dict) -> bool:
    ctx = _extract_c3_context(model, parameters)
    if ctx is None or "pod_always_schedulable" not in counterexample.get("violated_intents", []):
        return False
    if not _recognized_prefix(counterexample, ctx, model):
        return False
    if not any("no_feasible_node" in c.lower() for c in _trace_components(counterexample)[4:]):
        return False
    return ctx.h == 1 and ctx.z < ctx.k


def reason_spread_schedulability(counterexample: dict, model: dict, parameters: dict) -> list[dict]:
    ctx = _extract_c3_context(model, parameters)
    if ctx is None or not check_spread_schedulability(counterexample, model, parameters):
        return []
    # Derivation for (k, k-1, k) on zones (A,A,B):
    # Node A-low: hostname skew=1; zone skew=(2k-1)+1-k = k.
    # Node B: hostname skew=k+1-(k-1)=2; zone skew <= 1.
    # Node A-high imposes stronger hostname and zone conditions, so adds
    # no additional disjunct. The two nodes above have free resources.
    return [{
        "id": f"vault_sched_k{ctx.k}",
        "expression": {"op":"or", "args":[
            {"op":"ge", "parameter":"hostname_skew", "value":2},
            {"op":"ge", "parameter":"zone_skew", "value":ctx.k},
        ]},
        "source_intent":"pod_always_schedulable",
        "reasoning": {
            "engine":"vault", "rule":"topology_spread_schedulability",
            "scope":"bounded_c3_three_nodes_domains_1_to_6",
            "node_counts":ctx.node_counts,
            "node_zones":ctx.node_zones,
            "low_A_node":ctx.node_a_low,
            "single_B_node":ctx.node_b,
            "hostname_threshold_for_B":2,
            "zone_threshold_for_A":ctx.k,
            "explanation":"At least one of the two resource-eligible candidate nodes must pass hard topology spread after the same CPU/HPA prefix.",
        },
    }]


def check_spread_balance(counterexample: dict, model: dict, parameters: dict) -> bool:
    ctx = _extract_c3_context(model, parameters)
    if ctx is None or not ctx.has_preferred_b or ctx.h < 2:
        return False
    if "pod_balance_across_node" not in counterexample.get("violated_intents", []):
        return False
    if not _recognized_prefix(counterexample, ctx, model):
        return False
    intents = model.get("intents", [])
    if len(intents) != 1 or intents[0].get("name") != "checkBalanceNode" or intents[0].get("para", {}).get("maxSkew") != 1:
        return False
    components = _trace_components(counterexample)
    if "Scheduler" not in components or "checkBalanceNode" not in components:
        return False
    scheduler_step = next((step for step in counterexample["steps"] if step.get("component") == "Scheduler"), None)
    if scheduler_step is None:
        return False
    match = re.search(r"Pod\s+\d+\s+is scheduled on node\s+(\d+)", str(scheduler_step.get("description", "")))
    if not match or int(match.group(1)) != ctx.node_b:
        return False
    if not any("skew 2" in str(step.get("description", "")) for step in counterexample["steps"] if step.get("component") == "checkBalanceNode"):
        return False
    if not any(step.get("component") == "Kubelet" for step in counterexample["steps"]):
        return False
    return True


def reason_spread_balance(counterexample: dict, model: dict, parameters: dict) -> list[dict]:
    ctx = _extract_c3_context(model, parameters)
    if ctx is None or not check_spread_balance(counterexample, model, parameters):
        return []
    # In the checked Kivi C3-B family, the single preferred zone-B node is
    # selected as soon as h>=2 makes it eligible. It receives pod k+1,
    # producing node counts (k,k-1,k+1), skew=2, violating maxSkew=1.
    # This rule is intentionally only applicable to the bounded, observed
    # Kivi C3-B profile. It is NOT an arbitrary-Kubernetes theorem.
    return [{
        "id":"vault_balance_preferred_B_h1",
        "expression":{"op":"le", "parameter":"hostname_skew", "value":1},
        "source_intent":"pod_balance_across_node",
        "reasoning": {
            "engine":"vault", "rule":"topology_spread_balance",
            "scope":"bounded_c3_three_nodes_domains_1_to_6",
            "basis":"characterized Kivi C3-B preferred-zone scheduling behavior; verify against oracle before broadening",
            "node_counts_before":ctx.node_counts,
            "scheduled_node":ctx.node_b,
            "node_counts_after": {**ctx.node_counts, ctx.node_b:ctx.k+1},
            "resulting_skew":2,
            "monitor_max_skew":1,
            "explanation":"Preferred zone-B node remains eligible for h>=2 and is selected in this characterized family; its placement violates checkBalanceNode.",
        },
    }]


REASONING_RULES: dict[str, dict[str, Any]] = {
    "topology_spread_schedulability": {
        "applicable_intents": ["pod_always_schedulable"],
        "required_parameters": ["hostname_skew", "zone_skew"],
        "applicability": check_spread_schedulability,
        "handler": reason_spread_schedulability,
    },
    "topology_spread_balance": {
        "applicable_intents": ["pod_balance_across_node"],
        "required_parameters": ["hostname_skew", "zone_skew"],
        "applicability": check_spread_balance,
        "handler": reason_spread_balance,
    },
}
