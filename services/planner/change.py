"""Change evidence computed in code: what differs between the healthy baseline and the live template.

The Recovery Planner cannot see full templates, so without this it cannot tell
whether a rollback would change anything. template_change() compares the
baseline Pod template with the live one. Containers are matched by name, env
entries by name and ports by containerPort and protocol, so reordering a list
is not a change. Values are shown only for image, env, resources, ports and
probe fields. An env value whose name looks secret (TOKEN, KEY, PASSWORD,
SECRET and similar) or that comes from valueFrom is shown as "redacted". Every
shown value is cleaned and bounded, and a value that matches the
instruction-like rules (agent/untrusted.py) is withheld, so configuration text
cannot instruct the model. At most 20 changes are listed.

change_evidence() adds the image digests per container, the Deployment's
rollout facts (revision and generations) and a bound on how old the change can
be: a live template that differs from a baseline measured healthy at
healthy_observed_at changed after that time.
"""
import json
import re

from agent.untrusted import LABEL_LIMIT, clean, suspicious
from services.guard.common import json_sha256, utc_timestamp

MAX_CHANGES = 20
IMAGE_LIMIT = 256
SHOWN = frozenset({"image", "env", "resources", "ports", "probe"})
SECRET_NAME = re.compile(r"TOKEN|KEY|PASSWORD|PASSWD|SECRET|CREDENTIAL|PRIVATE|AUTH|CERT", re.IGNORECASE)
REDACTED = "redacted"
WITHHELD = "withheld"
_CONTAINER_LISTS = frozenset({"containers", "initContainers", "ephemeralContainers"})
_DIGEST = re.compile(r"@(sha256:[0-9a-f]{64})\Z")
_ENV = re.compile(r"\.env\[([^\]]*)\]")


def _identity(name, item):
    """The key that identifies one list item, or None when the list is compared as a whole."""
    if not isinstance(item, dict):
        return None
    if name in _CONTAINER_LISTS or name == "env":
        return item.get("name") if isinstance(item.get("name"), str) else None
    if name == "ports" and type(item.get("containerPort")) is int:
        return f"{item['containerPort']}/{item.get('protocol', 'TCP')}"
    return None


def _keyed(name, value):
    """A list as {identity: item} when every item has a distinct identity, else None."""
    if not isinstance(value, list):
        return None
    keys = [_identity(name, item) for item in value]
    if any(key is None for key in keys) or len(set(keys)) != len(keys):
        return None
    return dict(zip(keys, value, strict=True))


def field_class(path):
    if re.search(r"\.image$", path):
        return "image"
    if ".env[" in path:
        return "env"
    if ".resources" in path:
        return "resources"
    if re.search(r"(?:liveness|readiness|startup)Probe", path):
        return "probe"
    if ".ports" in path:
        return "ports"
    if re.search(r"\.(?:command|args)$", path):
        return "command"
    if path.startswith("metadata"):
        return "metadata"
    return "other"


def _shown(text, limit=LABEL_LIMIT):
    """One configuration value as prompt data: cleaned and bounded, or withheld if instruction-like."""
    text = text if isinstance(text, str) else json.dumps(text, sort_keys=True)
    return WITHHELD if suspicious(text) else clean(text, limit)


def _value(path, kind, value):
    if kind not in SHOWN or value is None:
        return None
    if kind == "env":
        name = _ENV.search(path).group(1)
        if SECRET_NAME.search(name) or ".valueFrom" in path or (isinstance(value, dict) and "valueFrom" in value):
            return REDACTED
        if isinstance(value, dict):
            value = value.get("value")
            if value is None:
                return None
    # An image reference with its digest is longer than a label.
    return _shown(value, IMAGE_LIMIT if kind == "image" else LABEL_LIMIT)


def _change(path, change, old=None, new=None):
    kind = field_class(path)
    item = {"path": clean(path, 200), "kind": change, "field_class": kind}
    for key, value in (("old", old), ("new", new)):
        shown = _value(path, kind, value)
        if shown is not None:
            item[key] = shown
    return item


def _diff(old, new, path, out, name=""):
    if old == new:
        return
    old_items, new_items = _keyed(name, old), _keyed(name, new)
    keyed = old_items is not None and new_items is not None
    if keyed:
        old, new = old_items, new_items
    elif not (isinstance(old, dict) and isinstance(new, dict)):
        out.append(_change(path, "modified", old, new))
        return
    for key in sorted(set(old) | set(new), key=str):
        # A list item is addressed by its identity, a field by its name.
        child = f"{path}[{key}]" if keyed else f"{path}.{key}" if path else str(key)
        if key not in old:
            out.append(_change(child, "added", new=new[key]))
        elif key not in new:
            out.append(_change(child, "removed", old=old[key]))
        else:
            # Only a field's name tells a nested list how its items are identified.
            _diff(old[key], new[key], child, out, "" if keyed else str(key))


def template_change(baseline_template, live_template):
    """What restoring baseline_template would change in live_template (both Pod templates)."""
    if not isinstance(baseline_template, dict) or not isinstance(live_template, dict):
        raise ValueError("Change evidence needs the baseline and live Pod templates")
    changes = []
    _diff(baseline_template, live_template, "", changes)
    changes.sort(key=lambda item: item["path"])
    return {"changed": bool(changes), "rollback_would_change_template": bool(changes),
            "change_count": len(changes), "changes": changes[:MAX_CHANGES],
            "truncated": len(changes) > MAX_CHANGES}


def _digest(image):
    match = _DIGEST.search(image) if isinstance(image, str) else None
    return match.group(1) if match else None


def images(baseline_template, live_template):
    """Image and digest per container name, baseline against live."""
    def by_name(template):
        containers = (template.get("spec") or {}).get("containers") or []
        return {item["name"]: item.get("image") for item in containers
                if isinstance(item, dict) and isinstance(item.get("name"), str)}
    old, new = by_name(baseline_template), by_name(live_template)
    result = []
    for name in sorted(set(old) | set(new))[:MAX_CHANGES]:
        result.append({"container": clean(name, LABEL_LIMIT),
                       "baseline_image": _shown(old[name], IMAGE_LIMIT) if isinstance(old.get(name), str) else None,
                       "live_image": _shown(new[name], IMAGE_LIMIT) if isinstance(new.get(name), str) else None,
                       "baseline_digest": _digest(old.get(name)), "live_digest": _digest(new.get(name)),
                       "image_changed": old.get(name) != new.get(name),
                       "digest_changed": _digest(old.get(name)) != _digest(new.get(name))})
    return result


def rollout(deployment):
    """The Deployment's own rollout facts: revision, generations and Progressing condition."""
    metadata, status = deployment.get("metadata") or {}, deployment.get("status") or {}
    revision = (metadata.get("annotations") or {}).get("deployment.kubernetes.io/revision")
    progressing = next((item for item in status.get("conditions") or []
                        if isinstance(item, dict) and item.get("type") == "Progressing"), {})
    return {"revision": _shown(revision) if isinstance(revision, str) else None,
            "generation": metadata.get("generation") if type(metadata.get("generation")) is int else None,
            "observed_generation": status.get("observedGeneration")
            if type(status.get("observedGeneration")) is int else None,
            "progressing_reason": _shown(progressing["reason"]) if isinstance(progressing.get("reason"), str) else None,
            "progressing_updated_at": _shown(progressing["lastUpdateTime"])
            if isinstance(progressing.get("lastUpdateTime"), str) else None}


def change_evidence(baseline, deployment, incident):
    """Code-computed change evidence for the Recovery Planner and the eligibility rule.

    change_age_bound_seconds is the time from the baseline's healthy
    observation to the incident: an upper bound on the age of any template
    change. It is None when the template has not changed.
    """
    template = deployment["spec"]["template"]
    diff = template_change(baseline["pod_template"], template)
    observed = utc_timestamp(incident["observed_at"], "observed_at")
    healthy = utc_timestamp(baseline["healthy_observed_at"], "baseline time")
    return {"computed_by": "code", "baseline_id": baseline["baseline_id"],
            "baseline_template_sha256": baseline["template_sha256"],
            "live_template_sha256": json_sha256(template), **diff,
            "images": images(baseline["pod_template"], template), "rollout": rollout(deployment),
            "changed_after": baseline["healthy_observed_at"],
            "change_age_bound_seconds": round((observed - healthy).total_seconds(), 3)
            if diff["changed"] else None}
