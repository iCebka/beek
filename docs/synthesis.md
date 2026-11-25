# Synthesis

[beek_synthesis.py](../beek_synthesis.py) separates text parsing, repair-problem construction, expression encoding, and Z3 invocation. The orchestrator supplies all search state on each call. `synthesize_repair` creates a new `Optimize` object; it does not keep an incremental solver between iterations.

## Problem contract

After a confirmed counterexample, `build_repair_problem` collects parameter domains and baseline/current values, all accumulated constraints, and previously refuted assignments. It validates each blocked assignment, deduplicates identical assignments, and adds the current refuted assignment. `counterexample.violated_intents` must be nonempty. Legacy callers may omit `current`, in which case blocking uses `original`.

This small complete problem is sufficient for the synthesizer; the `id` field is explanatory metadata:

```json
{
  "parameters": {
    "hostname_skew": {"original": 1, "current": 1, "domain": [1,2,3,4,5,6]},
    "zone_skew": {"original": 1, "current": 1, "domain": [1,2,3,4,5,6]}
  },
  "constraints": [
    {
      "id": "vault_sched_k5",
      "expression": {
        "op": "or",
        "args": [
          {"op": "ge", "parameter": "hostname_skew", "value": 2},
          {"op": "ge", "parameter": "zone_skew", "value": 5}
        ]
      }
    }
  ],
  "blocked_candidates": [{"hostname_skew": 1, "zone_skew": 1}],
  "objective": "min_changes_then_distance"
}
```

Notice the defaults at different layers: a declarative repair specification defaults to `none`; `build_repair_problem` defaults to `min_changes_then_distance`; `synthesize_repair` defaults a missing problem objective to `none`. The campaign resolves its objective and passes it explicitly, so the lower-level defaults do not change that choice.

## Encoding

Each parameter becomes a named Z3 integer. A finite domain is encoded as an OR of equalities, so sparse lists keep their gaps. Every constraint's `expression` is encoded recursively with the operators in [reasoning](reasoning.md).

Blocking a complete failed assignment `Cj` adds `ORᵢ(xᵢ != Cj[i])`. For `(1,1)`, that is `h != 1 OR z != 1`; it excludes only that pair, leaving either individual value available in another pair. Domains, learned constraints, and all blocking clauses are conjoined.

The solver expects a trusted problem from the builder/resolver. It checks empty domains and blocked parameter-name sets but does not repeat every spec-domain type check. The campaign separately checks a returned `SAT` candidate: it must be a dictionary with exactly the declared keys, exact integer values in their domains, and no match in the blocked list. Invalid output becomes `BeekCampaignError` and campaign `ERROR`. This check does not independently re-evaluate every learned expression; the supplied Z3 implementation provides that satisfaction condition.

## Optimization and outcomes

`Optimize` uses lexicographic priority. For `min_changes_then_distance`, objectives are changed-parameter count, sum of absolute distances from the fixed original assignment, then each variable's value in sorted parameter-name order. For two C3 parameters there are four objectives. Thus `(1,2)` precedes `(2,1)` when both have the same cost and are permitted. With the C3_B5 scheduling condition above, `(2,1)` is cheaper than `(1,5)` and is proposed first.

With objective `none`, there are no minimizing objectives, including no tie-break objectives. Z3 returns a satisfying assignment; candidate order and cost can vary with solver/process state. No timeout or seed is configured by this function.

| Z3 result | Returned dictionary |
| --- | --- |
| `sat` | `status: SAT`, complete `candidate`, `verification_status: NOT_RUN`, `metrics` |
| `unsat` | `status: UNSAT`, `candidate: null`, `verification_status: NOT_RUN`, `metrics` |
| Other result | `status: UNKNOWN`, `candidate: null`, `verification_status: NOT_RUN`, `reason`, `metrics` |

A candidate payload for the problem above is:

```json
{
  "status": "SAT",
  "candidate": {"hostname_skew": 2, "zone_skew": 1},
  "verification_status": "NOT_RUN"
}
```

This is an excerpt without timing fields. The candidate has not satisfied the intents merely by satisfying this problem. It must pass the next Kivi verification.

Metrics record build/check/total wall time, one check call, variable and assertion counts, objective count/name, and native Z3 statistics. A statistics-collection exception is stored as `collection_error`. `repair-problem.json` records the declarative problem; the code does not export an SMT-LIB file or the optimizer's complete textual formula.

A minimum-cost accepted repair requires sound exclusions, the intended fixed verifier context, and a successful optimum search. A conclusive no-repair result requires an exhausted bounded formula with sound exclusions. `UNKNOWN` and a candidate budget ending before another check establish neither result. See [repair model](repair-model.md).
