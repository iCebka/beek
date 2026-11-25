"""Declarative, validated repair-parameter input for Beek.

No solver, verification, or YAML mutation takes place in this module.
The spec describes *which* existing fields are repairable. The current
values always come from their input YAMLs, never from the spec.

The `model_binding` is required: a YAML parameter must already be represented
in the concrete Kivi model to qualify as a formally-verified repair variable.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

import yaml

SCHEMA = "beek.repair-spec.v1"
SUPPORTED_OBJECTIVES = {"none", "min_changes_then_distance"}
_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z_0-9-]*)(?:\[(\d+)\])?$")


def _require_dict(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _read_yaml(path: Path, name: str):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _require_dict(data, name)


def _walk(root: dict, path: str, context: str):
    """Read a conservative field path: 'spec.items[0].details'; no eval."""
    if not isinstance(path, str) or not path:
        raise ValueError(f"{context}: collection must be a nonempty dotted path")
    current = root
    for segment in path.split("."):
        match = _SEGMENT.fullmatch(segment)
        if match is None:
            raise ValueError(f"{context}: unsupported path segment {segment!r}")
        key, index = match.groups()
        if not isinstance(current, dict) or key not in current:
            raise ValueError(f"{context}: missing field {key!r} in {path!r}")
        current = current[key]
        if index is not None:
            if not isinstance(current, list) or int(index) >= len(current):
                raise ValueError(f"{context}: invalid index [{index}] in {path!r}")
            current = current[int(index)]
    return current


def _select(root: dict, binding: dict, context: str):
    path = binding.get("collection")
    collection = _walk(root, path, context)
    criteria = binding.get("match")
    if isinstance(collection, list):
        if not isinstance(criteria, dict) or not criteria:
            raise ValueError(f"{context}: selecting a list requires a nonempty 'match' mapping")
        matches = [(i, item) for i, item in enumerate(collection)
                   if isinstance(item, dict) and all(item.get(k) == v for k, v in criteria.items())]
        if len(matches) != 1:
            raise ValueError(f"{context}: expected exactly one match, found {len(matches)}")
        index, selected = matches[0]
    elif isinstance(collection, dict):
        if criteria not in (None, {}):
            raise ValueError(f"{context}: 'match' is only supported for list collections")
        index, selected = None, collection
    else:
        raise ValueError(f"{context}: collection must resolve to a dict or list")
    field = binding.get("field")
    if not isinstance(field, str) or not field or field not in selected:
        raise ValueError(f"{context}: field {field!r} is missing")
    return index, selected[field]


def _domain(spec, context: str) -> list[int]:
    if isinstance(spec, dict):
        if set(spec) - {"min", "max", "step"} or not {"min", "max"} <= set(spec):
            raise ValueError(f"{context}: domain range requires min and max, optional step")
        low, high, step = spec["min"], spec["max"], spec.get("step", 1)
        if any(type(x) is not int for x in (low, high, step)) or step <= 0 or low > high:
            raise ValueError(f"{context}: invalid integer domain bounds")
        if (high - low) // step > 100000:
            raise ValueError(f"{context}: domain exceeds 100001 values")
        values = list(range(low, high + 1, step))
    elif isinstance(spec, list):
        values = spec[:]
    else:
        raise ValueError(f"{context}: domain must be an integer list or a min/max range")
    if not values or any(type(v) is not int for v in values) or len(set(values)) != len(values):
        raise ValueError(f"{context}: domain must contain distinct integers")
    return values


def load_repair_spec(
    repair_spec_path: str | Path,
    input_files: Mapping[str, str | Path],
    model: dict,
    *,
    original_values: Mapping[str, int] | None = None,
) -> dict:
    """Resolve a declarative spec against the *current* YAML and Kivi model.

    Returns {schema, objective, parameters}, where each parameter contains
    original/current/domain, its YAML binding and its model binding.
    Fail closed if YAML and Kivi disagree, a selector is ambiguous, or a
    current/original value lies outside the declared finite domain.
    """
    path = Path(repair_spec_path).resolve(strict=True)
    spec = _read_yaml(path, "repair spec")
    if spec.get("schema") != SCHEMA:
        raise ValueError(f"Expected repair spec schema {SCHEMA!r}")
    objective = spec.get("objective", "none")
    if objective not in SUPPORTED_OBJECTIVES:
        raise ValueError(f"Unsupported objective: {objective!r}")
    entries = _require_dict(spec.get("parameters"), "parameters")
    if not entries:
        raise ValueError("At least one repair parameter is required")
    if original_values is not None and set(original_values) != set(entries):
        raise ValueError("original_values must contain exactly the declared parameters")
    if not isinstance(model, dict):
        raise TypeError("model must be a Kivi model dictionary")

    cached_documents = {}
    parameters = {}
    targets = set()
    for name, entry in entries.items():
        context = f"parameters.{name}"
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError(f"Invalid parameter name: {name!r}")
        entry = _require_dict(entry, context)
        if entry.get("type") != "int":
            raise ValueError(f"{context}: only type 'int' is currently supported")
        domain = _domain(entry.get("domain"), context)
        binding = _require_dict(entry.get("binding"), context + ".binding")
        model_binding = _require_dict(entry.get("model_binding"), context + ".model_binding")
        input_name = binding.get("input")
        if input_name not in input_files:
            raise ValueError(f"{context}: unknown input {input_name!r}")
        if input_name not in cached_documents:
            cached_documents[input_name] = _read_yaml(
                Path(input_files[input_name]).resolve(strict=True), f"{input_name} YAML"
            )
        doc = cached_documents[input_name]
        resource = binding.get("resource")
        if resource is not None:
            resource = _require_dict(resource, context + ".resource")
            if doc.get("kind") != resource.get("kind") or doc.get("metadata", {}).get("name") != resource.get("name"):
                raise ValueError(f"{context}: YAML resource does not match requested kind/name")
        index, current = _select(doc, binding, context + ".binding")
        _, model_current = _select(model, model_binding, context + ".model_binding")
        if type(current) is not int or type(model_current) is not int or current != model_current:
            raise ValueError(f"{context}: YAML value {current!r} != Kivi model value {model_current!r}")
        original = current if original_values is None else original_values[name]
        if type(original) is not int or current not in domain or original not in domain:
            raise ValueError(f"{context}: current and original must belong to the declared domain")
        # Detect multiple symbolic parameters writing the same field, even when
        # their match selectors are syntactically different.
        location = (input_name, binding["collection"], index, binding["field"])
        if location in targets:
            raise ValueError(f"{context}: multiple parameters address the same YAML field")
        targets.add(location)
        parameters[name] = {
            "original": original,
            "current": current,
            "domain": domain,
            "binding": binding,
            "model_binding": model_binding,
        }

    return {"schema": SCHEMA, "objective": objective, "parameters": parameters}
