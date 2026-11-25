# Repair model

The notation here follows manuscript Chapter 3. Runtime contracts come from [the specification resolver](../beek_repair_spec.py), [the loop](../beek_cegis.py), and [materialization](../beek_materialization.py).

## Instance and assignments

| Symbol | Meaning in Beek |
| --- | --- |
| `Y0` | Original six YAML documents |
| `Γ` | Fixed builder rules, Kivi controller/event semantics, and verification options |
| `P` | Nonempty set of editable integer parameters, each with an input binding, model binding, and finite domain `Dᵢ` |
| `I` | Checked intents, including Kivi's implicit schedulability assertion |
| `C` | Complete assignment of one domain value to each parameter |
| `C⁰` | Original assignment, read from the initial documents |
| `ApplyP(Y,C)` | Patch all fields of `P` in `Y`, preserving every other parsed field |
| `BuildΓ(Y)` | Build the concrete Kivi setup from `Y` |

The repair space is the Cartesian product `C(P) = D₁ × ... × Dₙ`. The original and current values must belong to the declared domains. C3 uses `hostname_skew` (`h`) and `zone_skew` (`z`) in `{1,...,6}`, giving 36 assignments.

An assignment is admissible when the builder accepts its materialized documents and every model binding holds the assigned value. The same integer must appear in the YAML field and its model field. The resolver rejects absent or ambiguous selectors and mismatched values. A domain alone does not encode every builder restriction: a candidate can satisfy the SMT problem yet fail reconstruction. The current loop stops with `ERROR` in that case.

The formal candidate model is `MΓ(C) = BuildΓ(ApplyP(Y0,C))`. Beek does not repair a PROMELA state directly. Within the intended instance, topology, initial state, events, intents, controller behavior, and all other unbound fields stay fixed. The general binding mechanism can point to other represented integer fields; changing the instance's intended scope requires deliberate parameter declarations and may invalidate Vault's guards.

## Valid and accepted

A valid repair is an admissible assignment whose model satisfies every intent for every execution admitted under `Γ`. An accepted repair is an admissible assignment whose verification completes with `PASS`.

`PASS` establishes the checked model's intents under that context and declared initial state. Its scope does not extend to other cluster sizes, initial states, unmodeled manifest fields, or arbitrary production executions. A Z3 `SAT` assignment only satisfies the current search formula. It becomes accepted after materialization, reconstruction, consistency validation, and Kivi verification.

The original assignment can already be valid. If it passes the first check, Beek returns it without synthesis or edits. A violation reports the first discovered failure; fixing it can expose another intent violation. [Verification](verification.md) explains the evidence used to distinguish these outcomes from incomplete searches and process failures.

## Learned constraints and finite exhaustion

After each confirmed violation, the loop excludes that complete assignment with a disjunction of parameter inequalities. Optional learned constraints exclude more assignments. A sound learned constraint excludes no valid repair. Beek checks the rule's applicability but does not prove soundness during the run.

`UNSAT` means the bounded search formula has no unblocked assignment. It establishes no repair in the declared space only when all learned constraints preserve every valid repair and the verifier/builder context is the intended one. With only confirmed-candidate blocking, this preservation condition follows from the refutations. `UNKNOWN`, `INCOMPLETE`, `ERROR`, and `BUDGET_EXHAUSTED` leave repairability undecided. The loop's final-budget check can stop before querying an exhausted formula.

## Cost

For original values `vᵢ` and assignment values `cᵢ`, Beek minimizes lexicographically:

1. `θ₁(C) = Σ [cᵢ != vᵢ]`, the number of changed fields.
2. `θ₂(C) = Σ |cᵢ - vᵢ|`, their total integer distance.

Additional objectives minimize each parameter value in ascending parameter-name order to break ties. Every distance uses `C⁰`, even when the latest materialization restores a field changed in a previous iteration.

If the solver finds the optimum of each remaining formula, all exclusions are sound, and a candidate passes verification, that accepted candidate has minimum cost among valid repairs. Without these conditions, a returned `PASS` still gives a verified repair, but the global minimum-cost argument does not follow. With objective `none`, Beek requests satisfiability only. See [synthesis](synthesis.md).
