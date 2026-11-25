# Inputs

The campaign requires exactly these six mapping keys in `input_files`: `cluster`, `deployment`, `initial_state`, `hpa`, `intents`, and `events`. Values are existing single-document YAML file paths. Original basenames are arbitrary; snapshots and materialized outputs use `<key>.yaml`. The repair specification is a separate seventh path. The [quickstart](quickstart.md) supplies a complete copyable example.

All six documents must load as mappings. PyYAML `safe_load` reads them; this is a narrow model adapter, not Kubernetes API schema validation. The tables below describe fields consumed by the code. Unknown fields are ignored unless an exact-key restriction is stated. Malformed nested types may raise Python `KeyError`, `TypeError`, or `AttributeError` as well as explicit `ValueError` exceptions.

## Cluster

| Field | Required value and effect |
| --- | --- |
| `kind` | `Cluster` |
| `nodes` | List with exactly three entries whose `role` is `worker` |
| `apiVersion`, images, control-plane patches, other node fields | Retained in snapshots; not translated |

The model's nodes, names, zones, and resources come from `initial_state`, not from this Kind manifest. The builder reads `cluster` and validates the worker count; it does not check a name-to-worker correspondence. Appendix B.1 says the cluster document does not enter the model; the current implementation does consume it as this topology precondition. Chapter 3.1's suggestion that it fixes zones is also narrower in code: zones come from the initial-state document.

## Deployment

| Field | Type, validation, and destination |
| --- | --- |
| `kind` | `Deployment` |
| `metadata.name` | Nonempty string; event target and HPA reference must match |
| `spec.replicas` | Positive integer; equals initial running Pod total and lies within HPA limits |
| `spec.template.metadata.labels` | Nonempty mapping; copied to template and active Pods |
| `spec.selector.matchLabels` | Exactly equal to template labels |
| `spec.template.spec.topologySpreadConstraints` | Nonempty list; exactly one `node` and one `zone` after translation |
| Each constraint's `maxSkew` | Positive integer; copied unchanged |
| `topologyKey` | `node` becomes Kivi `hostname`; `zone` remains `zone`; other keys rejected |
| `whenUnsatisfiable` | Must be `DoNotSchedule`; encoded as `0` |
| `labelSelector.matchLabels` | Nonempty mapping matching a subset of template labels; copied with `numMatchedLabel` |
| `spec.template.spec.affinity` | Optional; only the preferred node-affinity form below |
| Container image, env, container resource requests/limits, Deployment strategy | Preserved in YAML but not read into this model |

The builder sorts Kivi spread constraints with hostname first and zone second. Standard qualified Kubernetes topology keys are not accepted by this adapter; its supported input keys are the literal `node` and `zone` used by the case fixtures.

If `affinity` is present, it must contain exactly `nodeAffinity`. That mapping must contain exactly `preferredDuringSchedulingIgnoredDuringExecution`, a list of one preference. The preference has positive integer `weight <= 100` and exactly one `preference.matchExpressions` entry: `key: zone`, `operator: In`, and a single known zone in `values`. Kivi receives `isRequired: 0`, the weight, and `labels: {zone: <encoded ID>}`, with `numRules: 1`. Absence returns no affinity rules. Required affinity and other affinity families are rejected. Vault's supported affinity profile is stricter: weight 5 toward the single-node zone.

## Initial state and abstraction

The top-level keys must be exactly `nodes`, `zone_encoding`, and `abstraction`.

| Field | Contract |
| --- | --- |
| `nodes` | Exactly three mappings, in model node order |
| Each node | Exactly `name`, `zone`, `running_pods` |
| `name` | Unique nonempty string; validated but replaced with numeric model names |
| `zone` | Key in `zone_encoding` |
| `running_pods` | Integer >= 0; total must be positive and equal Deployment replicas |
| `zone_encoding` | Nonempty mapping to distinct exact integer IDs; no positivity check on IDs |
| `abstraction` | Exactly the six positive integer fields below |

```yaml
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
```

These are explicit integer model units. There is no conversion from Kubernetes millicores, memory suffixes, or container requests. Each node gets `cpu`, `memory`, `cpuLeft = node_cpu - running_pods * running_pod_cpu`, and `memLeft` by the corresponding memory formula. Negative remaining capacity is rejected. Active Pods get declared running consumption; the template gets `cpuRequested` and `memRequested` from the requested quantities.

Nodes receive IDs and names 1, 2, 3 with zone labels encoded by `zone_encoding`. Kivi's default processing adds a `hostname` label from each numeric node name. Active Pods point to these nodes with `loc`; each belongs to workload/template 1. One inactive spare Pod is appended with `loc: 4` and `status: 0`. Global Pod IDs start at 4; Kivi's trace numbering need not equal these global IDs. A Deployment with two ReplicaSet records is built after the Pod IDs. It uses `podTemplateId: 1` and its HPA specification. Controllers are `scheduler`, `hpa`, and `deployment`, with empty option dictionaries unless a reference generator is injected.

The builder accepts some three-node inputs beyond the characterized two-zone count pattern. That acceptance does not extend Vault's supported reasoning family; see its [guards](reasoning.md).

## HPA

`kind` must be `HorizontalPodAutoscaler`. `spec.scaleTargetRef.kind` must be `Deployment` and its name must match `metadata.name` of the supplied Deployment. Positive integer `minReplicas` and `maxReplicas` are required, with `max >= min`. The replica total plus the one spare slot must cover `maxReplicas`. The declared initial replica total equals Deployment `spec.replicas`, which must lie between the limits.

| `apiVersion` | CPU target |
| --- | --- |
| `autoscaling/v1` | Positive integer `spec.targetCPUUtilizationPercentage` |
| `autoscaling/v2` | Exactly one `spec.metrics` entry: `type: Resource`, `resource.name: cpu`, `resource.target.type: Utilization`, positive integer `averageUtilization` |

Other API versions or metric types are rejected. Kivi receives `isEnabled: 1`, `numMetrics: 1`, `metricNames: [0]`, `metricTypes: [1]`, `metricTargets: [target]`, and the two replica limits. Reference `apiVersion` and HPA metadata name are not used by this adapter. The builder permits a fixed-size HPA; Vault requires the specific one-Pod range `3k-1` to `3k`.

## Intents

The top level must contain exactly `intents`, a list. `intents: []` adds no explicit monitor; Kivi still injects schedulability. The only supported explicit entry is:

```yaml
intents:
  - {name: checkBalanceNode, maxSkew: 1}
```

Each entry must contain exactly `name` and `maxSkew`; duplicates are rejected. `maxSkew` is a positive integer. It becomes `{"name": "checkBalanceNode", "para": {"did": 1, "maxSkew": 1}}`. In reports the intent is named `pod_balance_across_node`; the default assertion is `pod_always_schedulable`. The `did: 1` is Kivi's Deployment index, not the global object ID.

## Events

The top level must contain exactly `events`, a list, which may be empty. Every entry is validated even if disabled:

```yaml
events:
  - id: workload_cpu_boost
    type: eventCpuChange
    enabled: true
    target: {kind: Deployment, name: h1-app}
    trigger: {after_stable: true}
```

`id` is a unique nonempty string; `type` must be `eventCpuChange`. Only `id`, `type`, `enabled`, `target`, and `trigger` are allowed. `enabled` defaults to `true` and must be Boolean. `target` contains exactly `kind` and `name` and must match the supplied Deployment. `trigger` contains exactly Boolean `after_stable`. There is no event magnitude or probability field.

The compiler requires exactly one model Deployment with an internal `name`. For an enabled entry it emits `{"name": "eventCpuChange", "para": {"targetDeployment": <internal name>}}`, adding `after_stable: true` only when requested. It replaces the model's events with a deep-copied compiled list. Disabled entries emit nothing. Multiple valid entries are accepted by the compiler; Vault requires exactly one enabled stable CPU event. These are admitted environmental behaviors, not predictions or cluster commands.

## Repair specification

This complete declaration binds both C3 parameters:

```yaml
schema: beek.repair-spec.v1
objective: min_changes_then_distance
parameters:
  hostname_skew:
    type: int
    domain: {min: 1, max: 6}
    binding:
      input: deployment
      resource: {kind: Deployment, name: h1-app}
      collection: spec.template.spec.topologySpreadConstraints
      match: {topologyKey: node}
      field: maxSkew
    model_binding:
      collection: setup.podTemplates[0].topoSpreadConstraints
      match: {topologyKey: hostname}
      field: maxSkew
  zone_skew:
    type: int
    domain: {min: 1, max: 6}
    binding:
      input: deployment
      resource: {kind: Deployment, name: h1-app}
      collection: spec.template.spec.topologySpreadConstraints
      match: {topologyKey: zone}
      field: maxSkew
    model_binding:
      collection: setup.podTemplates[0].topoSpreadConstraints
      match: {topologyKey: zone}
      field: maxSkew
```

`schema` must match exactly. `objective` defaults to `none`; valid values are `none` and `min_changes_then_distance`. `parameters` is a nonempty mapping with string keys accepted by `str.isidentifier()`. Only `type: int` is supported. Unknown spec/parameter fields are not explicitly rejected, but have no documented behavior. `current` and `original` are derived, so do not duplicate them in declarations.

Domains are either a nonempty list of distinct exact integers, such as `[1, 3, 6]`, or a mapping with exactly `min`, `max`, and optional `step` (default 1). Bounds are inclusive when reached by the step. Bounds and step must be exact integers, `step > 0`, and `min <= max`. Ranges are limited to 100001 generated values; explicit lists have no corresponding length check. Boolean values are rejected as integers. List order is preserved; Vault requires the exact ordered domain `[1,2,3,4,5,6]` for both parameters.

An input binding names an `input` present in the supplied path mapping. Optional `resource` checks root `kind` and `metadata.name`. `collection` is a nonempty dotted path, whose segments match `[A-Za-z_][A-Za-z_0-9-]*` with an optional nonnegative `[index]`. It supports no wildcards, arbitrary expressions, or nested selector keys. A list collection requires a nonempty `match` mapping of direct field equalities selecting exactly one dictionary. A dictionary collection permits absent or empty `match`. `field` is a present direct field name in the selected object. Model bindings use the same collection/match/field resolution against the model.

Input and model fields must contain equal exact integers. Current and original values must be in the domain. Initially `original = current`; later `original_values` must supply exactly the declared parameter names. Duplicate input targets are rejected by `(input, collection path, resolved list index, field)`. This detects differently written match criteria selecting the same target under the same collection path; it is not a general alias-analysis mechanism for different paths to one object.

Resolved data has this shape (one parameter excerpt):

```json
{
  "schema": "beek.repair-spec.v1",
  "objective": "min_changes_then_distance",
  "parameters": {
    "hostname_skew": {
      "original": 1,
      "current": 1,
      "domain": [1, 2, 3, 4, 5, 6],
      "binding": {
        "input": "deployment",
        "resource": {"kind": "Deployment", "name": "h1-app"},
        "collection": "spec.template.spec.topologySpreadConstraints",
        "match": {"topologyKey": "node"},
        "field": "maxSkew"
      },
      "model_binding": {
        "collection": "setup.podTemplates[0].topoSpreadConstraints",
        "match": {"topologyKey": "hostname"},
        "field": "maxSkew"
      }
    }
  }
}
```

See [materialization](materialization.md) for how these bindings control edits and [API reference](api-reference.md) for exact signatures.
