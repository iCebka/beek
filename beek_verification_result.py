"""Structured verification result for Beek's YAML -> Kivi/SPIN -> repair loop.

This module does NOT invoke Kivi, SPIN, read_raw_trace, parse_counterexample,
or Z3. It consumes the output of an already completed verify_and_save(...).

Repairable parameters and their bindings come exclusively from a repair_spec.yaml
input. Their actual current values are read from the verified YAML.

The six input YAMLs are snapshotted so a later repair can materialize a new
Deployment from the exact input associated with the verification result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import shutil
from typing import Mapping

from beek_repair_spec import load_repair_spec

INPUT_NAMES = ("cluster", "deployment", "initial_state", "hpa", "intents", "events")
STATUS_MAP = {
    "FAIL": "VIOLATION",
    "PASS": "PASS",
    "INCOMPLETE": "INCOMPLETE",
    "ERROR": "ERROR",
}


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _model_digest(model: dict) -> str:
    # Must match the digest computed in beek_verification_artifacts.py.
    canonical = json.dumps(model, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def create_verification_result(
    *,
    model: dict,
    artifacts: Mapping[str, str | Path],
    input_files: Mapping[str, str | Path],
    case: str,
    iteration: int = 0,
    repair_spec_path: str | Path,
    original_values: Mapping[str, int] | None = None,
    parent_run_id: str | None = None,
    overwrite: bool = False,
) -> tuple[dict, Path]:
    """Build and persist Beek's structured verification-result contract.

    Run this immediately after verify_and_save(...); it does not rerun SPIN.

    original_values are baseline values pinned across CEGIS iterations;
    current values are always read from the corresponding input YAML of *this* run.
    """
    if type(iteration) is not int or iteration < 0:
        raise ValueError("iteration must be a nonnegative integer")
    if not case or not isinstance(case, str):
        raise ValueError("case must be a nonempty string")
    if not isinstance(model, dict):
        raise TypeError("model must be a Kivi model dict")
    if iteration > 0 and original_values is None:
        raise ValueError("Later iterations must supply the baseline original_values")

    missing = set(INPUT_NAMES) - set(input_files)
    extras = set(input_files) - set(INPUT_NAMES)
    if missing or extras:
        raise ValueError(f"input_files must have exactly {INPUT_NAMES}; missing={missing}, extra={extras}")

    resolved_inputs = {name: Path(input_files[name]).resolve(strict=True) for name in INPUT_NAMES}
    for name, path in resolved_inputs.items():
        if not path.is_file():
            raise ValueError(f"{name}: expected a YAML file: {path}")

    metadata_path = Path(artifacts["metadata"]).resolve(strict=True)
    out = metadata_path.parent
    result_path = out / "verification-result.json"
    snapshots_dir = out / "input_yamls"
    if result_path.exists() and not overwrite:
        raise FileExistsError(f"Result already exists: {result_path}")
    if snapshots_dir.exists() and any(snapshots_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"Snapshots already exist: {snapshots_dir}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("schema") != "beek.verification-artifacts.v1":
        raise ValueError("Unexpected metadata schema")
    raw_status = metadata.get("result")
    if raw_status not in STATUS_MAP:
        raise ValueError(f"Unknown verifier status: {raw_status!r}")
    status = STATUS_MAP[raw_status]
    expected_digest = _model_digest(model)
    if metadata.get("model_sha256") != expected_digest:
        raise ValueError("Model hash does not match the model verified by Kivi")

    failure_count = metadata.get("kivi_failure_count")
    pan_errors = metadata.get("pan_errors")
    if status == "VIOLATION" and (not isinstance(failure_count, int) or failure_count < 1 or
                                   not isinstance(pan_errors, int) or pan_errors < 1):
        raise ValueError("A VIOLATION requires a confirmed Kivi failure and pan error")
    if status == "PASS" and (failure_count != 0 or pan_errors != 0):
        raise ValueError("A PASS requires zero Kivi failures and pan errors")

    repair_spec_path = Path(repair_spec_path).resolve(strict=True)
    parsed_spec = load_repair_spec(
        repair_spec_path, resolved_inputs, model,
        original_values=original_values,
    )
    parameters = parsed_spec["parameters"]
    names = tuple(parameters)

    snapshots_dir.mkdir(parents=True, exist_ok=True)
    inputs = {}
    for name, path in resolved_inputs.items():
        snapshot = snapshots_dir / f"{name}.yaml"
        shutil.copy2(path, snapshot)
        source_hash = _digest(path)
        snapshot_hash = _digest(snapshot)
        if source_hash != snapshot_hash:
            raise RuntimeError(f"Source YAML changed while copying {name}; aborting")
        inputs[name] = {
            "source_path": str(path),
            "snapshot_path": str(snapshot),
            "sha256": snapshot_hash,
        }

    # Preserve the *declarative* synthesis input together with the verified YAMLs.
    spec_snapshot = snapshots_dir / "repair_spec.yaml"
    shutil.copy2(repair_spec_path, spec_snapshot)
    if _digest(repair_spec_path) != _digest(spec_snapshot):
        raise RuntimeError("Repair specification changed while copying")

    raw_trail = Path(artifacts["raw_trail"]).resolve(strict=True)
    if raw_trail.parent != out:
        raise ValueError("raw-trail.txt and verification.json must belong to the same run")

    files = {
        name: str(Path(path).resolve())
        for name, path in artifacts.items()
    }
    # Each file path is an output of verify_and_save, not a fresh verification.
    record = {
        "schema": "beek.verification-result.v2",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "run": {
            "id": metadata.get("case_id"),
            "case": case,
            "iteration": iteration,
            "parent_run_id": parent_run_id,
        },
        "verification": {
            "status": status,
            "checked_intents": metadata.get("checked_intents", []),
            "violated_intents": metadata.get("violating_intents_inferred_from_trace", []),
            "violated_intents_source": "inferred_from_kivi_trace",
            "failure_count": failure_count,
            "pan_errors": pan_errors,
            "reason": metadata.get("reason"),
            "model_sha256": expected_digest,
        },
        "inputs": inputs,
        "repair_parameters": parameters,
        "repair_spec": {
            "schema": parsed_spec["schema"],
            "objective": parsed_spec["objective"],
            "source_path": str(repair_spec_path),
            "snapshot_path": str(spec_snapshot),
            "sha256": _digest(spec_snapshot),
        },
        "materialization": {
            "kind": "copy_and_patch_yaml_bindings",
            "source_snapshots": {
                name: info["snapshot_path"] for name, info in inputs.items()
            },
            "allowed_parameters": list(names),
        },
        "artifacts": files,
    }
    result_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record, result_path
