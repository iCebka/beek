# Reasoning

The reasoning callback transforms `(parsed_counterexample, current_model, solver_parameters)` into a list of constraint records. Solver parameters contain `original`, `current`, and `domain`; YAML bindings are not passed by the loop. Constraints restrict future proposals. Kivi still verifies each proposal.

## Engine and rule dispatch

Use `partial(learn_constraints, engine="vault")` from [beek_reasoning_engine.py](../beek_reasoning_engine.py#L25), or `learn_constraints` itself for the default engine. `learn_constraints_fn=None` disables the callback. `vault` is implemented; `llm` and `general` print a selection message and return an empty `RecordedConstraints` list with metrics. Unknown engine names raise `ValueError`. The placeholder branches return before validating the counterexample/model types.

Vault requires dictionary arguments and a list-valued `violated_intents`. It examines each entry of `REASONING_RULES` in registry order. Entries declare `applicable_intents`, `required_parameters`, `applicability`, and `handler`. A rule is `SKIPPED` for `intent_mismatch`, `missing_required_parameters`, or `applicability_rejected`; it is `EXECUTED` when its handler runs, even if the handler returns no constraints. Exceptions mark the local rule report `ERROR` and re-raise. Since the callback then returns no report, the loop may have no `reasoning.json` for that error.

The result is a list subclass with a `reasoning_metrics` attribute. Its entries carry `id`, `expression`, `source_intent`, and `reasoning` provenance. The loop deduplicates the entire record by sorted-key JSON, not by rule ID or logical equivalence. It keeps all unique records across iterations. `reasoning.json` stores dispatch metrics; the full learned constraint records are in `repair-problem.json` and `campaign-summary.json`.

## Shared C3 guards

The implemented tests are in [_extract_c3_context](../beek_reasoning_rules.py#L123) and [_recognized_prefix](../beek_reasoning_rules.py#L102). They abstain outside this profile:

| Area | Required by the current guards |
| --- | --- |
| Root/setup | Exact root keys `setup`, `controllers`, `events`, with optional `intents`; exact setup keys `nodes`, `pods`, `d`, `podTemplates` |
| Controllers | Exactly `scheduler`, `deployment`, `hpa`, all with empty options |
| Nodes | Three distinct integer IDs, active status, labels containing only `zone`; two nodes in zone A and one in zone B |
| Counts | Zone-B node has `k` Pods, `2 <= k <= 6`; zone-A nodes have uniquely `k` and `k-1` |
| Resources | Homogeneous CPU/memory capacity; nonnegative integer capacity/free fields within capacity; low-A and B nodes have enough free requested CPU and memory; no extra node scheduling fields |
| Template | One template; only recognized fields; positive CPU/memory requests, nonempty labels, exactly two hard spread constraints with matching labels and counts |
| Parameters | Exactly `hostname_skew` and `zone_skew`; exact ordered domains `[1,2,3,4,5,6]`; positive originals/current values; current values in domains and equal model skews |
| Affinity | Absent or one preferred rule with exactly `isRequired: 0`, `weight: 5`, and labels for zone B; `numRules` agrees |
| Pods | Exactly the counted active Pods of workload/template 1 with template labels and valid locations, plus exactly one inactive slot |
| Deployment/HPA | One Deployment, current and desired replicas `3k-1`, template 1; HPA enabled with min `3k-1`, max `3k` |
| Event | One `eventCpuChange`, `after_stable is True`, targeting the Deployment ID |
| Intents | Absent/empty or one `checkBalanceNode` with `did: 1`, `maxSkew: 1` |
| Trace prefix | At least five steps; first components exactly `CPU Change`, `HPA`, `Deployment`, `Deployment`; recognized one-Pod HPA scale-up and creation messages |
| CPU evidence | First recognized CPU-change record targets the high-A node and reports remaining CPU equal to its initial `cpuLeft` minus delta |

The checks are syntactic and model-conditioned. For example, they do not recalculate every node's free resources from Pod consumption, and they do not prove a controller transition relation. Use the supplied builder and inspect the source before broadening applicability.

## Scheduling rule

Registry ID: `topology_spread_schedulability`. Constraint ID: `vault_sched_k<k>`, for example `vault_sched_k5`.

In addition to the shared guards, it requires violated intent `pod_always_schedulable`, a component containing `no_feasible_node` after the four-step prefix, and current `h=1, z<k`. The test searches later components; it does not require that component to be the final step.

For counts `(k,k-1,k)` on zones `(A,A,B)`, the modeled candidate placement conditions are:

| New Pod goes to | Hostname skew | Zone skew | Required limit |
| --- | --- | --- | --- |
| High-A node | 2 | k | `h >= 2 and z >= k` |
| Low-A node | 1 | k | `z >= k` |
| B node | 2 | 1 | `h >= 2` |

The low-A and B nodes have resources by the guards. The first condition adds no alternative beyond those two. The returned condition is `κ_sched(k) = (h >= 2) OR (z >= k)`:

```json
{
  "id": "vault_sched_k5",
  "expression": {
    "op": "or",
    "args": [
      {"op": "ge", "parameter": "hostname_skew", "value": 2},
      {"op": "ge", "parameter": "zone_skew", "value": 5}
    ]
  },
  "source_intent": "pod_always_schedulable"
}
```

This is an excerpt; actual records also include `reasoning` with counts, zones, candidate nodes, thresholds, scope, and explanation. The theoretical derivation is in manuscript §4.4.3. For original C3 (`k=2`) either relaxation can permit scheduling. The rule does not require both limits to increase.

## Balance rule

Registry ID: `topology_spread_balance`. Constraint ID: `vault_balance_preferred_B_h1`.

It requires the shared profile with preferred B affinity, current `h>=2`, violated intent `pod_balance_across_node`, exactly one balance monitor with limit 1, and the same recognized prefix. It also requires an exact `Scheduler` component whose first such step says the Pod is scheduled on the B node, a `Kubelet` component, and a `checkBalanceNode` step containing `skew 2`.

For `h>=2`, B is feasible in this profile and receives the new Pod under the characterized Kivi scheduling behavior. Counts become `(k,k-1,k+1)`, with difference 2. The handler returns:

```json
{
  "id": "vault_balance_preferred_B_h1",
  "expression": {"op": "le", "parameter": "hostname_skew", "value": 1},
  "source_intent": "pod_balance_across_node"
}
```

The complete record also includes before/after counts, selected node, skew, monitor threshold, and the observed-family basis. For `z<k`, B is the only feasible node when `h>=2`. For `z>=k`, excluding all `h>=2` assignments also relies on Kivi selecting the preferred B node when alternatives exist. The runtime does not prove that selection behavior for arbitrary configurations. Manuscript §4.4.4 gives the argument and §6.1.2 reports its comparison against all bounded assignments.

Together with the domains, both learned conditions give `h=1, z>=k`. A campaign only accumulates both if it encounters both kinds of counterexample. Some campaigns pass before a balance counterexample occurs.

## Constraint language and fallback

Comparison leaves have `op` in `eq`, `ne`, `lt`, `le`, `gt`, `ge`, a `parameter` name, and `value`. Boolean nodes use `and` or `or` with nonempty `args`, or `not` with `arg`. The encoder recursively maps them to Z3. There are no arithmetic-expression nodes or parameter-to-parameter comparison nodes in this protocol. A custom rule must produce well-formed expressions over declared names; validation is partial and malformed expressions can fail during encoding or metrics evaluation.

With no applicable rule, the returned list is empty and search continues by blocking the just-refuted complete assignment. That blocking occurs even when the engine is absent. Soundness requires a rule to exclude no valid repair; re-verifying the candidates it keeps does not reveal valid candidates it wrongly removed. The manuscript's 216 exhaustive checks support these two rules for six studied instances. This documentation run checked rule behavior and a representative campaign; it did not repeat that full formal sweep.
