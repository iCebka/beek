"""Materialize a Beek/Z3 candidate as a new, independent set of YAML inputs.

Intended pipeline:
    verification_result -> Z3 candidate -> materialize_repair
    -> build_kivi_model(next_input_files...) -> verify_and_save

This module does not invoke Kivi, SPIN, the solver, or kubectl. It treats the
recorded, hash-checked YAML snapshots as immutable input for each iteration.

Requirements: PyYAML (already used by beek_model_builder.py).
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Mapping

import yaml

from beek_repair_spec import _select, _walk

INPUT_NAMES = ("cluster", "deployment", "initial_state", "hpa", "intents", "events")
RESULT_SCHEMA = "beek.verification-result.v2"
MATERIALIZATION_SCHEMA = "beek.materialization.v1"


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _require_mapping(value, context: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a mapping")
    return value


def _load_yaml(path: Path, name: str) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    return _require_mapping(document, name)


def _locate_bound_field(document: dict, binding: dict, context: str):
    """Get unique target dict + field name using the exact repair-spec selector semantics."""
    collection = _walk(document, binding.get("collection"), context)
    # _select enforces a unique match, present field and supported collection shape.
    index, value = _select(document, binding, context)
    target = collection[index] if index is not None else collection
    return target, binding["field"], index, value


def _validated_inputs(verification_result: dict):
    inputs = _require_mapping(verification_result.get("inputs"), "verification_result.inputs")
    if set(inputs) != set(INPUT_NAMES):
        raise ValueError(f"Expected precisely these six YAML inputs: {INPUT_NAMES}")
    sources: dict[str, Path] = {}
    for name in INPUT_NAMES:
        entry = _require_mapping(inputs[name], f"inputs.{name}")
        source = Path(entry["snapshot_path"]).resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"Input snapshot is not a file: {source}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or _sha256(source) != digest:
            raise ValueError(f"Snapshot hash mismatch for {name}; refusing to materialize")
        sources[name] = source
    if len(set(sources.values())) != len(sources):
        raise ValueError("Input snapshots must be six distinct files")
    materialization = _require_mapping(
        verification_result.get("materialization"), "verification_result.materialization"
    )
    if materialization.get("kind") != "copy_and_patch_yaml_bindings":
        raise ValueError("Unsupported materialization strategy")
    declared_sources = materialization.get("source_snapshots")
    if not isinstance(declared_sources, dict) or set(declared_sources) != set(INPUT_NAMES):
        raise ValueError("Malformed materialization.source_snapshots")
    for name, source in sources.items():
        if Path(declared_sources[name]).resolve(strict=True) != source:
            raise ValueError(f"Snapshot/source mismatch for {name}")

    spec_entry = _require_mapping(verification_result.get("repair_spec"), "repair_spec")
    spec_source = Path(spec_entry["snapshot_path"]).resolve(strict=True)
    if not spec_source.is_file() or _sha256(spec_source) != spec_entry.get("sha256"):
        raise ValueError("Repair specification snapshot has been modified")
    return sources, spec_source


def materialize_repair(
    *,
    verification_result: Mapping,
    candidate: Mapping[str, int],
    output_dir: str | Path,
) -> tuple[dict[str, Path], dict]:
    """Write the next iteration's YAMLs from a verified run and Z3 candidate.

    Args:
        verification_result: dictionary returned by create_verification_result.
        candidate: complete symbolic assignment from synthesize_repair(...)["candidate"].
        output_dir: NEW directory for next iteration's inputs (must not already exist).

    Returns:
        (next_input_files, materialization): six Paths directly consumable by
        build_kivi_model, plus provenance, edits and repair_spec_path. The new
        directory also holds repair_spec.yaml and materialization.json.

    Does not rewrite the source snapshots, original YAMLs, or the Kivi model.
    The edited YAML may be reformatted by PyYAML; its parsed contents differ
    from the original only at explicitly authorized bindings.
    """
    record = _require_mapping(verification_result, "verification_result")
    if record.get("schema") != RESULT_SCHEMA:
        raise ValueError(f"Expected verification result schema {RESULT_SCHEMA!r}")
    status = _require_mapping(record.get("verification"), "verification").get("status")
    if status != "VIOLATION":
        raise ValueError(f"Can only materialize a candidate after VIOLATION, got {status!r}")
    run = _require_mapping(record.get("run"), "run")
    if type(run.get("iteration")) is not int or run["iteration"] < 0:
        raise ValueError("run.iteration must be a nonnegative integer")

    parameters = _require_mapping(record.get("repair_parameters"), "repair_parameters")
    if not parameters:
        raise ValueError("No repair parameters declared")
    candidate = _require_mapping(candidate, "candidate")
    if set(candidate) != set(parameters):
        raise ValueError(
            f"Candidate must assign exactly {sorted(parameters)}; got {sorted(candidate)}"
        )
    allowed = _require_mapping(record.get("materialization"), "materialization").get("allowed_parameters")
    if not isinstance(allowed, list) or len(allowed) != len(set(allowed)) or set(allowed) != set(parameters):
        raise ValueError("Materialization's allowlist does not match repair parameters")

    sources, spec_source = _validated_inputs(record)
    # Refuse to overwrite or mix with previous executions. Resolve parent but
    # do not create the final output directory until every check passes.
    destination = Path(output_dir).expanduser().absolute()
    if destination.exists():
        raise FileExistsError(f"Materialization destination must be new: {destination}")
    for src in (*sources.values(), spec_source):
        if destination == src or destination in src.parents:
            raise ValueError(f"Destination would overwrite a source snapshot: {destination}")

    updated: dict[str, dict] = {}
    changes: list[dict] = []
    seen_targets = set()
    for name, info in parameters.items():
        info = _require_mapping(info, f"repair_parameters.{name}")
        value = candidate[name]
        domain = info.get("domain")
        if type(value) is not int or not isinstance(domain, list) or value not in domain:
            raise ValueError(f"{name}: candidate value {value!r} is not an allowed integer")
        original = info.get("original")
        current = info.get("current")
        if type(original) is not int or original not in domain or type(current) is not int or current not in domain:
            raise ValueError(f"{name}: invalid original/current values")
        binding = _require_mapping(info.get("binding"), f"{name}.binding")
        input_name = binding.get("input")
        if input_name not in sources:
            raise ValueError(f"{name}: input {input_name!r} is not among the six YAMLs")
        if input_name not in updated:
            updated[input_name] = _load_yaml(sources[input_name], f"input {input_name}")
        doc = updated[input_name]
        resource = binding.get("resource")
        if resource is not None:
            resource = _require_mapping(resource, f"{name}.binding.resource")
            if doc.get("kind") != resource.get("kind") or doc.get("metadata", {}).get("name") != resource.get("name"):
                raise ValueError(f"{name}: YAML resource kind/name differs from binding")
        target, field, index, observed = _locate_bound_field(doc, binding, name)
        key = (input_name, binding.get("collection"), index, field)
        if key in seen_targets:
            raise ValueError(f"Two symbolic parameters address the same field: {key}")
        seen_targets.add(key)
        if type(observed) is not int or observed != current:
            raise ValueError(f"{name}: YAML snapshot value {observed!r} differs from verified current {current!r}")
        target[field] = value
        changes.append({
            "parameter": name,
            "input": input_name,
            "binding": deepcopy(binding),
            "from": current,
            "to": value,
            "changed": value != current,
        })

    if not any(item["changed"] for item in changes):
        raise ValueError("Candidate is identical to the verified configuration; refusing a no-op repair")

    # Validate every resulting YAML's parsed structure before publishing a
    # single file; writes are staged and moved into place as one directory.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}.staging-", dir=destination.parent) as tmp:
        stage = Path(tmp)
        staged_paths = {}
        for name, source in sources.items():
            out = stage / f"{name}.yaml"
            if name in updated:
                with out.open("w", encoding="utf-8") as f:
                    yaml.safe_dump(updated[name], f, sort_keys=False, allow_unicode=True)
                if _load_yaml(out, name) != updated[name]:
                    raise RuntimeError(f"Serialized YAML differs from in-memory patch for {name}")
            else:
                shutil.copyfile(source, out)  # untouched YAMLs stay byte-identical
                if _sha256(out) != _sha256(source):
                    raise RuntimeError(f"Unchanged YAML {name} was unexpectedly modified")
            staged_paths[name] = out

        spec_out = stage / "repair_spec.yaml"
        shutil.copyfile(spec_source, spec_out)
        if _sha256(spec_out) != _sha256(spec_source):
            raise RuntimeError("Repair specification copy changed")

        # The same six inputs are returned; additional files are metadata only.
        manifest = {
            "schema": MATERIALIZATION_SCHEMA,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_run_id": run.get("id"),
            "source_case": run.get("case"),
            "source_iteration": run["iteration"],
            "next_iteration": run["iteration"] + 1,
            "candidate": dict(candidate),
            "changes": changes,
            "repair_spec_sha256": _sha256(spec_out),
            "inputs": {
                name: {
                    "source_sha256": _sha256(sources[name]),
                    "output_sha256": _sha256(staged_paths[name]),
                }
                for name in INPUT_NAMES
            },
        }
        (stage / "materialization.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        # tempfile's context will clean stage if any check or rename fails.
        # Ensure no destination has appeared between the preflight and rename.
        if destination.exists():
            raise FileExistsError(f"Destination was created concurrently: {destination}")
        os.rename(stage, destination)

    next_input_files = {name: destination / f"{name}.yaml" for name in INPUT_NAMES}
    manifest["output_dir"] = str(destination)
    manifest["repair_spec_path"] = str(destination / "repair_spec.yaml")
    manifest["materialization_path"] = str(destination / "materialization.json")
    return next_input_files, manifest
