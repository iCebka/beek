"""Compile Beek's supported potential-event catalog into Kivi's event schema.

Currently implemented:
    eventCpuChange targeting the Deployment described by the Kubernetes YAML.

This intentionally does not implement Kubernetes-to-Kivi state translation;
that is handled by the model builder. Unknown event fields are rejected rather
than silently altering the intended verification semantics.
"""

from copy import deepcopy
from pathlib import Path

import yaml


def apply_event_catalog(model, events_yaml_path, deployment_yaml_path):
    """Return a copy of ``model`` whose events come solely from ``events.yaml``.

    The current model adapter supports one Deployment. The deployment name in
    Kubernetes YAML is matched against the catalog, then linked to the existing
    Kivi deployment's internal ``name`` identifier. It must not be replaced by
    the Kubernetes text name directly.
    """
    if not isinstance(model, dict):
        raise TypeError("model must be a Kivi configuration dictionary")

    with Path(events_yaml_path).open(encoding="utf-8") as stream:
        catalog = yaml.safe_load(stream)
    with Path(deployment_yaml_path).open(encoding="utf-8") as stream:
        deployment_yaml = yaml.safe_load(stream)

    if not isinstance(catalog, dict) or set(catalog) != {"events"}:
        raise ValueError("events.yaml must contain exactly the 'events' key")
    if not isinstance(catalog["events"], list):
        raise ValueError("'events' must be a list")

    if not isinstance(deployment_yaml, dict) or deployment_yaml.get("kind") != "Deployment":
        raise ValueError("Expected a Kubernetes Deployment manifest")
    deployment_name = deployment_yaml.get("metadata", {}).get("name")
    if not isinstance(deployment_name, str) or not deployment_name:
        raise ValueError("The Deployment manifest must have metadata.name")

    deployments = model.get("setup", {}).get("d", [])
    if not isinstance(deployments, list) or len(deployments) != 1:
        raise ValueError("This adapter currently requires exactly one Kivi Deployment")
    kivi_deployment_name = deployments[0].get("name")
    if kivi_deployment_name is None:
        raise ValueError("Kivi Deployment lacks its internal 'name' identifier")

    compiled = []
    seen_ids = set()
    for index, event in enumerate(catalog["events"]):
        if not isinstance(event, dict):
            raise ValueError(f"events[{index}] must be a mapping")
        unsupported = set(event) - {"id", "type", "enabled", "target", "trigger"}
        if unsupported:
            raise ValueError(f"events[{index}] has unsupported fields: {sorted(unsupported)}")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError(f"events[{index}].id must be a nonempty string")
        if event_id in seen_ids:
            raise ValueError(f"Duplicate event ID: {event_id}")
        seen_ids.add(event_id)
        if event.get("type") != "eventCpuChange":
            raise ValueError(f"Unsupported event type: {event.get('type')!r}")
        enabled = event.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"events[{index}].enabled must be boolean")

        target = event.get("target")
        if not isinstance(target, dict) or set(target) != {"kind", "name"}:
            raise ValueError(f"events[{index}].target requires kind and name")
        if target != {"kind": "Deployment", "name": deployment_name}:
            raise ValueError(
                f"events[{index}] targets {target!r}, expected Deployment/{deployment_name}"
            )

        trigger = event.get("trigger")
        if not isinstance(trigger, dict) or set(trigger) != {"after_stable"}:
            raise ValueError(f"events[{index}].trigger requires after_stable")
        if not isinstance(trigger["after_stable"], bool):
            raise ValueError(f"events[{index}].trigger.after_stable must be boolean")

        if enabled:
            kivi_event = {
                "name": "eventCpuChange",
                "para": {"targetDeployment": kivi_deployment_name},
            }
            if trigger["after_stable"]:
                kivi_event["after_stable"] = True
            compiled.append(kivi_event)

    result = deepcopy(model)
    result["events"] = compiled  # Replace, never append S3's existing events.
    return result
