# Quickstart

This example runs C3_B5 through both reasoning rules and final verification. Its inputs follow the local C3 fixtures and manuscript Appendix B, with cluster and container fields omitted where the model does not read them. It creates all seven YAML documents under a new temporary directory. Run from the repository directory, after the [dependency checks](installation.md).

The Python block was executed during documentation validation against an isolated copy of the local Kivi checkout. It returned `PASS`, with three verifications and accepted assignment `{"hostname_skew": 1, "zone_skew": 5}`. That observation concerns the checked environment; inspect your own summary and process output.

## Run

Select a writable Kivi checkout if the variable is not already set. Use a fresh demo directory on every run:

```bash
read -r -p "Absolute path to a writable Kivi checkout: " BEEK_KIVI_ROOT
export BEEK_KIVI_ROOT
BEEK_DEMO_DIR="$(mktemp -d)"
export BEEK_DEMO_DIR
```

```python
import json
import os
from functools import partial
from pathlib import Path

import yaml
from beek_cegis import run_beek_cegis
from beek_model_builder import build_kivi_model, verify_kivi_model
from beek_reasoning_engine import learn_constraints
from beek_repair_spec import load_repair_spec
from beek_synthesis import (
    read_raw_trace, parse_counterexample, build_repair_problem, synthesize_repair,
)

work = Path(os.environ["BEEK_DEMO_DIR"]).resolve(strict=True)
kivi = Path(os.environ["BEEK_KIVI_ROOT"]).resolve(strict=True)
case_dir = work / "case"
case_dir.mkdir()  # fails if inputs from an earlier run are present

documents = {
    "cluster": """
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - {role: control-plane}
  - {role: worker}
  - {role: worker}
  - {role: worker}
""",
    "deployment": """
apiVersion: apps/v1
kind: Deployment
metadata: {name: h1-app}
spec:
  replicas: 14
  selector: {matchLabels: {run: h1-app}}
  template:
    metadata: {labels: {run: h1-app}}
    spec:
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: node
          whenUnsatisfiable: DoNotSchedule
          labelSelector: {matchLabels: {run: h1-app}}
        - maxSkew: 1
          topologyKey: zone
          whenUnsatisfiable: DoNotSchedule
          labelSelector: {matchLabels: {run: h1-app}}
      affinity:
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 5
              preference:
                matchExpressions:
                  - {key: zone, operator: In, values: [B]}
""",
    "initial_state": """
nodes:
  - {name: worker-1, zone: A, running_pods: 5}
  - {name: worker-2, zone: A, running_pods: 4}
  - {name: worker-3, zone: B, running_pods: 5}
zone_encoding: {A: 1, B: 2}
abstraction:
  node_cpu: 64
  node_memory: 64
  running_pod_cpu: 8
  running_pod_memory: 8
  pod_cpu_requested: 8
  pod_memory_requested: 8
""",
    "hpa": """
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: h1-app-hpa}
spec:
  scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: h1-app}
  minReplicas: 14
  maxReplicas: 15
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: {type: Utilization, averageUtilization: 91}
""",
    "intents": """
intents:
  - {name: checkBalanceNode, maxSkew: 1}
""",
    "events": """
events:
  - id: workload_cpu_boost
    type: eventCpuChange
    enabled: true
    target: {kind: Deployment, name: h1-app}
    trigger: {after_stable: true}
""",
}
inputs = {}
for name, text in documents.items():
    path = case_dir / f"{name}.yaml"
    path.write_text(text.lstrip(), encoding="utf-8")
    inputs[name] = path

parameters = {}
for name, input_key, model_key in (
    ("hostname_skew", "node", "hostname"),
    ("zone_skew", "zone", "zone"),
):
    parameters[name] = {
        "type": "int",
        "domain": {"min": 1, "max": 6},
        "binding": {
            "input": "deployment",
            "resource": {"kind": "Deployment", "name": "h1-app"},
            "collection": "spec.template.spec.topologySpreadConstraints",
            "match": {"topologyKey": input_key},
            "field": "maxSkew",
        },
        "model_binding": {
            "collection": "setup.podTemplates[0].topoSpreadConstraints",
            "match": {"topologyKey": model_key},
            "field": "maxSkew",
        },
    }
spec = case_dir / "repair_spec.yaml"
spec.write_text(yaml.safe_dump({
    "schema": "beek.repair-spec.v1",
    "objective": "min_changes_then_distance",
    "parameters": parameters,
}, sort_keys=False), encoding="utf-8")

model = build_kivi_model(
    inputs["cluster"], inputs["deployment"], inputs["initial_state"],
    inputs["hpa"], inputs["intents"], inputs["events"],
)
resolved = load_repair_spec(spec, inputs, model)
assert resolved["parameters"]["hostname_skew"]["current"] == 1

summary = run_beek_cegis(
    model, spec, 35,
    input_files=inputs,
    kivi_root=kivi,
    case="C3_B5",
    output_root=work / "runs",
    build_model_fn=build_kivi_model,
    verify_fn=verify_kivi_model,
    read_raw_trace_fn=read_raw_trace,
    parse_counterexample_fn=parse_counterexample,
    build_repair_problem_fn=build_repair_problem,
    synthesize_repair_fn=synthesize_repair,
    learn_constraints_fn=partial(learn_constraints, engine="vault"),
)
print(json.dumps({key: summary[key] for key in (
    "status", "reason", "verified_candidate", "total_verifications",
    "final_input_files", "summary_path",
)}, indent=2))
```

The minimal Deployment above is a model input, with no container image or resource request manifest. It is accepted by this adapter but is not a complete deployable Kubernetes workload. The model's resources come from `initial_state.yaml`. For the exact input contract, read [inputs](inputs.md).

## Inspect the result

The printed `summary_path` points to:

```text
<demo directory>/runs/C3_B5/<generated campaign id>/campaign-summary.json
```

Read `status` and `reason` first. On `PASS`, `verified_candidate` is the accepted assignment and `final_input_files` maps the six input names to the accepted verification's snapshots. Open `final_input_files["deployment"]` to inspect the repaired fields. `verified_candidates` counts new candidates; `total_verifications` includes the initial model. See the full [artifact tree](campaign-artifacts.md).

The observed sequence was scheduling violation at `(1,1)`, balance violation at `(2,1)`, and `PASS` at `(1,5)`. The original input files stayed unchanged. A different terminal outcome requires inspection of its reason; terminal outcomes are listed in [architecture](architecture.md).

## Select reasoning and optimization

Change these arguments in a new run. All four combinations were executed during documentation validation:

| Treatment | `learn_constraints_fn` | `objective` argument |
| --- | --- | --- |
| T00 | `None` | `"none"` |
| T10 | `partial(learn_constraints, engine="vault")` | `"none"` |
| T01 | `None` | `"min_changes_then_distance"` |
| T11 | `partial(learn_constraints, engine="vault")` | `"min_changes_then_distance"` |

Omitting `objective` uses `"from_spec"`, as in the example. Passing `None` also disables optimization. Blocking and re-verification occur in every treatment. `llm` and `general` return no constraints; use `None` when you want no reasoner. With objective `none`, a particular candidate order or minimum cost is not promised.

The historical experiment corpus and its wrappers are external to this tree. Repeating a campaign with these modules is possible with the example; reproducing the full study also needs the assets described in [evaluation](evaluation.md).
