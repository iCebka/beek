"""Build and verify concrete C3 / C3_B family setups using Kivi's original engine.

Inputs:
  * Kind cluster manifest: worker count
  * initial_state.yaml: observed/synthetic snapshot and explicit abstraction
  * Deployment YAML: desired replicas, pod labels, affinity, topology spread
  * HPA YAML: min/max replicas and CPU utilization target
  * intents.yaml: additional Kivi intents (default schedulability is added by Kivi)
  * events.yaml: potential environmental events (delegated to beek_events.py)

The model is rebuilt from these inputs. Controller names are the built-in
Kivi controllers; no C3 generator or initial state is inherited.
This limited adapter is intended for a single homogeneous Deployment on three
workers, with one additional free Pod slot for HPA scale-up.
"""

import os
import sys
from copy import deepcopy
from pathlib import Path

import yaml

from beek_events import apply_event_catalog


def _load_yaml(path):
    with Path(path).open(encoding="utf-8") as stream:
        obj = yaml.safe_load(stream)
    if not isinstance(obj, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return obj


def _positive_int(value, field, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError(f"{field} must be an integer >= {0 if zero else 1}")
    return value


def _kivi_spread_constraints(pod_spec, template_labels):
    yaml_constraints = pod_spec.get("topologySpreadConstraints")
    if not isinstance(yaml_constraints, list) or not yaml_constraints:
        raise ValueError("Deployment requires topologySpreadConstraints")

    mapping = {"node": "hostname", "zone": "zone"}
    output = []
    seen = set()
    for c in yaml_constraints:
        if not isinstance(c, dict):
            raise ValueError("Topology spread constraint must be a mapping")
        key = c.get("topologyKey")
        if key not in mapping:
            raise ValueError(f"Unsupported topologyKey: {key!r}")
        key = mapping[key]
        if key in seen:
            raise ValueError(f"Duplicate topologyKey: {key}")
        seen.add(key)
        # C3 family uses hard constraints; soft constraints may be added in future.
        if c.get("whenUnsatisfiable") != "DoNotSchedule":
            raise ValueError("Only DoNotSchedule is supported in C3 family")
        labels = c.get("labelSelector", {}).get("matchLabels")
        if not isinstance(labels, dict) or not labels:
            raise ValueError("Each spread constraint requires matchLabels")
        if any(template_labels.get(k) != v for k, v in labels.items()):
            raise ValueError("Topology selector does not match Pod template labels")
        output.append({
            "maxSkew": _positive_int(c.get("maxSkew"), "topology.maxSkew"),
            "topologyKey": key,
            "whenUnsatisfiable": 0,
            "numMatchedLabel": len(labels),
            "labels": deepcopy(labels),
        })
    if seen != {"hostname", "zone"}:
        raise ValueError("C3 family requires one hostname and one zone constraint")
    # Kivi's known C3 convention, for reproducibility.
    return sorted(output, key=lambda c: (c["topologyKey"] != "hostname", c["topologyKey"]))


def _kivi_affinity(pod_spec, zone_encoding):
    affinity = pod_spec.get("affinity")
    if affinity is None:
        return []
    if not isinstance(affinity, dict) or set(affinity) != {"nodeAffinity"}:
        raise ValueError("Only nodeAffinity is supported")
    node_affinity = affinity["nodeAffinity"]
    if not isinstance(node_affinity, dict):
        raise ValueError("nodeAffinity must be a mapping")
    if set(node_affinity) != {"preferredDuringSchedulingIgnoredDuringExecution"}:
        raise ValueError("Only preferred nodeAffinity is supported")
    preferences = node_affinity["preferredDuringSchedulingIgnoredDuringExecution"]
    if not isinstance(preferences, list) or len(preferences) != 1:
        raise ValueError("This C3-family adapter supports one preference")
    p = preferences[0]
    expressions = p.get("preference", {}).get("matchExpressions", [])
    if len(expressions) != 1:
        raise ValueError("Expected one affinity matchExpression")
    expr = expressions[0]
    if expr.get("key") != "zone" or expr.get("operator") != "In":
        raise ValueError("Only zone In [...] affinity is supported")
    values = expr.get("values")
    if not isinstance(values, list) or len(values) != 1 or values[0] not in zone_encoding:
        raise ValueError("Affinity must target a known single zone")
    weight = _positive_int(p.get("weight"), "affinity weight")
    if weight > 100:
        raise ValueError("Affinity weight must be <= 100")
    return [{
        "isRequired": 0,
        "weight": weight,
        "labels": {"zone": zone_encoding[values[0]]},
    }]


def _kivi_hpa(hpa_yaml, deployment_name):
    if hpa_yaml.get("kind") != "HorizontalPodAutoscaler":
        raise ValueError("Expected HorizontalPodAutoscaler YAML")
    spec = hpa_yaml.get("spec", {})
    ref = spec.get("scaleTargetRef", {})
    if ref.get("kind") != "Deployment" or ref.get("name") != deployment_name:
        raise ValueError("HPA target must be the supplied Deployment")
    min_replicas = _positive_int(spec.get("minReplicas"), "HPA minReplicas")
    max_replicas = _positive_int(spec.get("maxReplicas"), "HPA maxReplicas")
    if max_replicas < min_replicas:
        raise ValueError("HPA maxReplicas must be >= minReplicas")

    if hpa_yaml.get("apiVersion") == "autoscaling/v1":
        target = spec.get("targetCPUUtilizationPercentage")
    elif hpa_yaml.get("apiVersion") == "autoscaling/v2":
        metrics = spec.get("metrics")
        if not isinstance(metrics, list) or len(metrics) != 1:
            raise ValueError("Only one HPA CPU utilization metric is supported")
        metric = metrics[0]
        if metric.get("type") != "Resource":
            raise ValueError("Only Resource CPU HPA metric supported")
        resource = metric.get("resource", {})
        if resource.get("name") != "cpu" or resource.get("target", {}).get("type") != "Utilization":
            raise ValueError("Only CPU Utilization HPA metric supported")
        target = resource["target"].get("averageUtilization")
    else:
        raise ValueError("Only autoscaling/v1 and autoscaling/v2 HPA supported")
    _positive_int(target, "HPA CPU utilization target")
    return {
        "isEnabled": 1,
        "numMetrics": 1,
        "metricNames": [0],
        "metricTypes": [1],
        "metricTargets": [target],
        "minReplicas": min_replicas,
        "maxReplicas": max_replicas,
    }


def _kivi_intents(intents_yaml):
    if set(intents_yaml) != {"intents"} or not isinstance(intents_yaml["intents"], list):
        raise ValueError("intents.yaml must contain an intents list")
    output = []
    seen = set()
    for i in intents_yaml["intents"]:
        if not isinstance(i, dict) or i.get("name") != "checkBalanceNode":
            raise ValueError("Only the additional checkBalanceNode intent is supported")
        if set(i) != {"name", "maxSkew"} or i["name"] in seen:
            raise ValueError("Unexpected/duplicate checkBalanceNode intent")
        seen.add(i["name"])
        output.append({
            "name": "checkBalanceNode",
            "para": {"did": 1, "maxSkew": _positive_int(i["maxSkew"], "intent.maxSkew")},
        })
    return output


def build_c3_from_yaml(
    cluster_yaml_path,
    deployment_yaml_path,
    initial_state_yaml_path,
    hpa_yaml_path,
    intents_yaml_path,
    *,
    reference_generator=None,
):
    """Build a concrete one-Deployment C3-family model from declared inputs.

    Does not infer initial placement from the Deployment. Does not modify or
    implement any Kivi controller. Optional reference generator is for tests.
    """
    kind_yaml = _load_yaml(cluster_yaml_path)
    deploy_yaml = _load_yaml(deployment_yaml_path)
    state_yaml = _load_yaml(initial_state_yaml_path)
    hpa_yaml = _load_yaml(hpa_yaml_path)
    intents_yaml = _load_yaml(intents_yaml_path)

    if kind_yaml.get("kind") != "Cluster":
        raise ValueError("Expected a Kind Cluster YAML")
    workers = [n for n in kind_yaml.get("nodes", []) if n.get("role") == "worker"]
    if len(workers) != 3:
        raise ValueError("C3 family requires exactly three worker nodes")

    if deploy_yaml.get("kind") != "Deployment":
        raise ValueError("Expected Kubernetes Deployment YAML")
    deployment_name = deploy_yaml.get("metadata", {}).get("name")
    if not isinstance(deployment_name, str) or not deployment_name:
        raise ValueError("Deployment metadata.name is required")
    deploy_spec = deploy_yaml.get("spec", {})
    replicas_desired = _positive_int(deploy_spec.get("replicas"), "Deployment spec.replicas")
    template = deploy_spec.get("template", {})
    labels = template.get("metadata", {}).get("labels")
    if not isinstance(labels, dict) or not labels:
        raise ValueError("Pod template labels are required")
    if deploy_spec.get("selector", {}).get("matchLabels") != labels:
        raise ValueError("Only an exact selector/pod-label match is supported")
    pod_spec = template.get("spec", {})

    if set(state_yaml) != {"nodes", "zone_encoding", "abstraction"}:
        raise ValueError("initial_state requires nodes, zone_encoding, abstraction")
    state_nodes = state_yaml["nodes"]
    zones = state_yaml["zone_encoding"]
    a = state_yaml["abstraction"]
    if not isinstance(state_nodes, list) or len(state_nodes) != 3:
        raise ValueError("initial_state.nodes must contain three nodes")
    if not isinstance(zones, dict) or not zones or any(type(v) is not int for v in zones.values()):
        raise ValueError("zone_encoding must map zone names to integer IDs")
    if len(set(zones.values())) != len(zones):
        raise ValueError("zone_encoding IDs must be distinct")
    resource_keys = {
        "node_cpu", "node_memory", "running_pod_cpu", "running_pod_memory",
        "pod_cpu_requested", "pod_memory_requested",
    }
    if not isinstance(a, dict) or set(a) != resource_keys:
        raise ValueError(f"abstraction must contain exactly {sorted(resource_keys)}")
    for key in resource_keys:
        _positive_int(a[key], f"abstraction.{key}")

    nodes = []
    pods = []
    names = set()
    total_running = 0
    for i, sn in enumerate(state_nodes, 1):
        if not isinstance(sn, dict) or set(sn) != {"name", "zone", "running_pods"}:
            raise ValueError("Each initial node requires name, zone and running_pods")
        if not isinstance(sn["name"], str) or not sn["name"] or sn["name"] in names:
            raise ValueError("Initial node names must be unique nonempty strings")
        names.add(sn["name"])
        if sn["zone"] not in zones:
            raise ValueError(f"Unknown zone {sn['zone']}")
        n_pods = _positive_int(sn["running_pods"], "running_pods", zero=True)
        left_cpu = a["node_cpu"] - n_pods * a["running_pod_cpu"]
        left_mem = a["node_memory"] - n_pods * a["running_pod_memory"]
        if left_cpu < 0 or left_mem < 0:
            raise ValueError(f"Initial node {sn['name']} exceeds abstract resources")
        nodes.append({
            "id": i, "name": i,
            "cpu": a["node_cpu"], "memory": a["node_memory"],
            "cpuLeft": left_cpu, "memLeft": left_mem,
            "numPod": n_pods, "status": 1,
            "labels": {"zone": zones[sn["zone"]]},
        })
        for _ in range(n_pods):
            pods.append({
                "loc": i,
                "workloadType": 1, "workloadId": 1, "podTemplateId": 1,
                "status": 1, "labels": deepcopy(labels),
                "cpu": a["running_pod_cpu"],
                "memory": a["running_pod_memory"], "important": 0,
            })
        total_running += n_pods

    # Reserve one free slot for a possible HPA-created Pod, as in native C3.
    if total_running < 1:
        raise ValueError("C3 family requires at least one running Pod")
    pods.append({
        "loc": len(nodes) + 1,  # native C3 uses loc=4 for the spare slot
        "workloadType": 1, "workloadId": 1, "podTemplateId": 1,
        "status": 0,
        "cpu": a["running_pod_cpu"],
        "memory": a["running_pod_memory"], "important": 0,
    })
    for j, pod in enumerate(pods, len(nodes) + 1):
        pod["id"] = j

    hpa = _kivi_hpa(hpa_yaml, deployment_name)
    if total_running + 1 < hpa["maxReplicas"]:
        raise ValueError("One free Pod slot insufficient for HPA maxReplicas")
    if not (hpa["minReplicas"] <= replicas_desired <= hpa["maxReplicas"]):
        raise ValueError("Deployment replicas must lie within HPA min/max for the stable C3 family")
    if total_running != replicas_desired:
        raise ValueError(
            f"For this stable C3-family snapshot, running Pods={total_running} "
            f"must equal Deployment spec.replicas={replicas_desired}"
        )

    spread = _kivi_spread_constraints(pod_spec, labels)
    affinity_rules = _kivi_affinity(pod_spec, zones)
    pod_template = {
        "cpuRequested": a["pod_cpu_requested"],
        "memRequested": a["pod_memory_requested"],
        "labels": deepcopy(labels),
        "topoSpreadConstraints": spread,
        "numTopoSpreadConstraints": len(spread),
    }
    if affinity_rules:
        pod_template["affinityRules"] = affinity_rules
        pod_template["numRules"] = len(affinity_rules)

    d_id = len(nodes) + len(pods) + 1
    deployment = {
        "id": d_id, "name": d_id, "status": 1, "curVersion": 0,
        "replicaSets": [
            {"id": d_id + 1, "deploymentId": 1, "replicas": total_running,
             "specReplicas": replicas_desired, "version": 0,
             "podIds": list(range(1, total_running + 1))},
            {"id": d_id + 2, "deploymentId": 1},
        ],
        "specReplicas": replicas_desired, "replicas": total_running,
        "podTemplateId": 1, "hpaSpec": hpa,
    }
    intents = _kivi_intents(intents_yaml)

    # Built-in Kivi controllers: already implemented in Kivi's PROMELA.
    # No case-generator import or dependence on Jupyter's sys.argv.
    controllers = {
        "scheduler": {},
        "hpa": {},
        "deployment": {},
    }

    # Optional injection preserves existing test/reference support.
    if reference_generator is not None:
        reference = reference_generator(3)
        if not isinstance(reference, dict) or not isinstance(reference.get("controllers"), dict):
            raise ValueError("Reference generator returned an invalid controller configuration")
        for name in controllers:
            if name not in reference["controllers"]:
                raise ValueError(f"Reference generator lacks controller: {name}")
            controllers[name] = deepcopy(reference["controllers"][name])

    model = {
        "setup": {
            "nodes": nodes,
            "pods": pods,
            "d": [deployment],
            "podTemplates": [pod_template],
        },
        "controllers": controllers,
        "events": [],
    }
    if intents:
        model["intents"] = intents
    return model


def build_kivi_model(
    cluster_yaml_path,
    deployment_yaml_path,
    initial_state_yaml_path,
    hpa_yaml_path,
    intents_yaml_path,
    events_yaml_path,
    *,
    reference_generator=None,
):
    model = build_c3_from_yaml(
        cluster_yaml_path,
        deployment_yaml_path,
        initial_state_yaml_path,
        hpa_yaml_path,
        intents_yaml_path,
        reference_generator=reference_generator,
    )
    return apply_event_catalog(
        model,
        events_yaml_path,
        deployment_yaml_path,
    )


def verify_kivi_model(model, kivi_root, case_id="beek_c3_family"):
    """Verify concrete Kivi model through VeriferOperator (no case regeneration).

    Like Kivi's own runner, this uses a process-global working directory and
    overwrites generated files. Do not run concurrent validations in one Kivi
    checkout. inspect failure logs even when no failure is reported.
    """
    root = Path(kivi_root).resolve()
    if not isinstance(model, dict):
        raise TypeError("Expected Kivi model dictionary")
    if not (root / "kivi" / "verifier_operator.py").is_file():
        raise FileNotFoundError(root / "kivi" / "verifier_operator.py")
    if not (root / "libs" / "Spin" / "Src" / "spin").is_file():
        raise FileNotFoundError("Missing Kivi SPIN binary")
    kivi_path = str(root / "kivi")
    if kivi_path not in sys.path:
        sys.path.insert(0, kivi_path)

    # util.py invokes argparse on first import. Jupyter's -f argument would
    # otherwise be interpreted as a Kivi argument.
    original_argv = sys.argv.copy()
    try:
        sys.argv = ["kivi_runner.py", "-c", "c3", "-o"]
        import util
        from verifier_operator import VeriferOperator
    finally:
        sys.argv = original_argv

    flags = {
        "original": True,
        "simulation": False,
        "all_violation": False,
        "intents_group": 0,
        "random": False,
        "file_debug": 0,
    }
    saved_flags = {k: getattr(util.args, k) for k in flags if hasattr(util.args, k)}
    old_cwd = Path.cwd()
    try:
        for k, v in flags.items():
            if k in saved_flags:
                setattr(util.args, k, v)
        os.chdir(root / "bin")
        pml_path, result_path = util.generate_dir(str(root), case_id, 3)
        verifier = VeriferOperator(deepcopy(model), str(root), result_path, pml_path, case_id + "_3")
        verifier.operator()
        return verifier
    finally:
        os.chdir(old_cwd)
        for k, v in saved_flags.items():
            setattr(util.args, k, v)
