"""Live image inventory for the CVE gate: which digests run, and how old their scans are (P5.02, #72).

Reads only Deployments and Pods in one namespace (kubectl get, read-only), or
the same JSON from --from-json for offline use. For each container it records
the spec image and the repository digest it resolves to: the spec image when
it is pinned by digest, otherwise the one repo digest the Pods report in
containerStatuses[].imageID. Anything else (a bare config ID, Pods that
disagree, no Pod) stays UNKNOWN; it is never guessed.
With --platform-indexes, recorded registry index bytes are hash-verified and
Nodes are also read to prove spec/Pod equivalence for each node's platform.

Each resolved digest gets the age and state of its newest scan in --scans
(FRESH, STALE, FAILED or MISSING). --scan-queue lists the running digests,
those new since --previous or not FRESH first, at most 20, in the format
watch_security.py --inventory reads, so the monitor scans what is running
instead of a fixed list. With --journal a
digest that is not FRESH raises the dashboard's SCAN_STALE alert (and a FRESH
one resolves it). Exit 0 when every container is resolved and FRESH, 2
otherwise. Only the context kind-sentinel-boutique-connected is allowed;
anything naming sentinel-lab is refused.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.image_scans import newest_scans
from services.image_security import canonical_image

SCHEMA = "sentinel-image-inventory-v1"
ALLOWED_CONTEXTS = frozenset({"kind-sentinel-boutique-connected"})
PROTECTED = "sentinel-lab"
DEFAULT_MAX_AGE = 86400
QUEUE_LIMIT = 20  # watch_security.load_inventory accepts 1-20 images


class Refused(ValueError):
    """The inventory will not read this cluster."""


def check_context(context):
    if not isinstance(context, str) or not context.strip():
        raise Refused("An explicit --context is required")
    if PROTECTED in context.lower():
        raise Refused("The protected sentinel-lab cluster is never used")
    if context not in ALLOWED_CONTEXTS:
        raise Refused("Context is not in the inventory allowlist: " + ", ".join(sorted(ALLOWED_CONTEXTS)))
    return context


def read_live(context, namespace, *, runner=subprocess.run, include_nodes=False):
    """Deployments and Pods of one namespace, optionally Nodes, read-only."""
    check_context(context)
    result = runner(["kubectl", "--context", context, "--namespace", namespace, "get",
                     "deployments,pods,nodes" if include_nodes else "deployments,pods",
                     "--output", "json"], capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    if result.returncode:
        raise RuntimeError("kubectl get deployments,pods failed")
    return json.loads(result.stdout)


def _repo_digest(image_id):
    """The repository digest in a containerStatuses imageID, or None (a bare config ID is not one)."""
    if not isinstance(image_id, str):
        return None
    for prefix in ("docker-pullable://", "docker://"):
        image_id = image_id.removeprefix(prefix)
    return canonical_image(image_id)


def _containers(spec):
    for group in ("initContainers", "containers"):
        for container in spec.get(group, []) or []:
            if isinstance(container, dict) and isinstance(container.get("name"), str):
                yield container["name"], container.get("image")


def _statuses(pod):
    status = pod.get("status", {}) or {}
    for group in ("initContainerStatuses", "containerStatuses"):
        for item in status.get(group, []) or []:
            if isinstance(item, dict):
                yield item.get("name"), item.get("imageID")


def _selects(selector, labels):
    match = (selector or {}).get("matchLabels") or {}
    return bool(match) and not (selector or {}).get("matchExpressions") and all(
        labels.get(key) == value for key, value in match.items())


def load_platform_manifests(path):
    """Verify recorded registry index bytes against their pinned digests; no network or aliases."""
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict) or document.get("schema") != "sentinel-platform-indexes-v1":
        raise ValueError("Expected sentinel-platform-indexes-v1")
    result = {}
    for record in document["indexes"]:
        image = canonical_image(record["image"])
        if image is None or image in result:
            raise ValueError("Invalid or duplicate index reference")
        raw = (path.parent / record["index_file"]).read_bytes()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != image.split("@")[1]:
            raise ValueError("Recorded registry index digest mismatch")
        index = json.loads(raw)
        if index.get("mediaType") not in ("application/vnd.oci.image.index.v1+json",
                                          "application/vnd.docker.distribution.manifest.list.v2+json"):
            raise ValueError("Expected a registry image index")
        platforms = {}
        for child in index["manifests"]:
            platform = child.get("platform", {})
            key = (platform.get("os"), platform.get("architecture"))
            if not all(isinstance(part, str) and part != "unknown" for part in key):
                continue
            manifest = canonical_image(image.split("@")[0] + "@" + child["digest"])
            if manifest is None:
                raise ValueError("Invalid platform manifest")
            # NodeInfo has no variant: multiple arm variants cannot establish equivalence.
            platforms[key] = manifest if key not in platforms else None
        result[image] = platforms
    return result


def workloads(objects, *, platform_manifests=None):
    """One entry per Deployment container, plus containers of Pods no Deployment selects."""
    items = objects.get("items", []) if isinstance(objects, dict) else []
    deployments = [item for item in items if isinstance(item, dict) and item.get("kind") == "Deployment"]
    pods = [item for item in items if isinstance(item, dict) and item.get("kind") == "Pod"]
    nodes = {item.get("metadata", {}).get("name"): item.get("status", {}).get("nodeInfo", {})
             for item in items if isinstance(item, dict) and item.get("kind") == "Node"}
    platform_manifests = platform_manifests or {}
    claimed, result = set(), []

    def entry(kind, name, container, image, observed):
        pinned = canonical_image(image)
        digests = {digest for digest, node in observed if digest is not None}
        digest = pinned if pinned is not None else (next(iter(digests)) if len(digests) == 1 and all(d for d, n in observed)
                                                     else None)
        proof = []
        if pinned is not None and digests - {pinned}:
            digest = None  # the Pods run something other than the pinned spec: a rollout or a drift
            for observed_digest, node in observed:
                info = nodes.get(node, {})
                platform = (info.get("operatingSystem"), info.get("architecture"))
                manifest = platform_manifests.get(pinned, {}).get(platform)
                actual = platform_manifests.get(observed_digest, {}).get(platform, observed_digest)
                if (manifest is None or manifest != actual or observed_digest is None
                        or pinned.split("@")[0] != observed_digest.split("@")[0]):
                    break
                proof.append({"node": node, "platform": "/".join(platform), "observed": observed_digest,
                              "manifest": manifest})
            else:
                digest = pinned
        result = {"kind": kind, "name": name, "container": container, "image": image if isinstance(image, str) else None,
                  "digest": digest, "status": "RESOLVED" if digest is not None else "UNKNOWN"}
        if digest is not None and proof:
            result["platform_resolution"] = proof
        return result

    for deployment in sorted(deployments, key=lambda item: item.get("metadata", {}).get("name", "")):
        name = deployment.get("metadata", {}).get("name")
        selector = deployment.get("spec", {}).get("selector")
        owned = [pod for pod in pods if _selects(selector, pod.get("metadata", {}).get("labels", {}) or {})]
        claimed.update(id(pod) for pod in owned)
        spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
        for container, image in _containers(spec):
            observed = [(_repo_digest(image_id), pod.get("spec", {}).get("nodeName"))
                        for pod in owned for status_name, image_id in _statuses(pod)
                        if status_name == container]
            result.append(entry("Deployment", name, container, image, observed))
    for pod in sorted(pods, key=lambda item: item.get("metadata", {}).get("name", "")):
        if id(pod) in claimed:
            continue
        for container, image in _containers(pod.get("spec", {})):
            observed = [(_repo_digest(image_id), pod.get("spec", {}).get("nodeName"))
                        for status_name, image_id in _statuses(pod) if status_name == container]
            result.append(entry("Pod", pod.get("metadata", {}).get("name"), container, image, observed))
    return result


def build(objects, *, namespace, scans, previous=None, max_age=DEFAULT_MAX_AGE, now, platform_manifests=None):
    entries = workloads(objects, platform_manifests=platform_manifests)
    digests = sorted({item["digest"] for item in entries if item["digest"] is not None})
    newest = newest_scans(scans, digests) if scans is not None else {}
    known = set(previous.get("digests", {})) if isinstance(previous, dict) else set()
    records = {}
    for digest in digests:
        scan = newest.get(digest)
        age, state = None, "MISSING"
        if scan is not None and scan.get("status") == "UNKNOWN":
            state = "FAILED"
        elif scan is not None:
            stamp = datetime.fromisoformat(scan["scanned_at"].replace("Z", "+00:00"))
            age = round((now - stamp).total_seconds())
            state = "FRESH" if 0 <= age <= max_age else "STALE"
        records[digest] = {"scan": state, "scan_age_seconds": age, "new": digest not in known}
    # Digests that need a scan first, then the rest, so the monitor also keeps fresh scans fresh.
    queue = sorted(digests, key=lambda digest: (not (records[digest]["new"] or records[digest]["scan"] != "FRESH"), digest))
    return {"schema": SCHEMA, "namespace": namespace,
            "captured_at": now.isoformat().replace("+00:00", "Z"), "max_scan_age_seconds": max_age,
            "workloads": entries, "digests": records,
            "unresolved": sum(item["digest"] is None for item in entries),
            "scan_queue": queue[:QUEUE_LIMIT], "scan_queue_truncated": len(queue) > QUEUE_LIMIT}


def alerts(journal, inventory):
    """SCAN_STALE for each digest whose scan is not FRESH, and for unresolved containers."""
    from runtime.security_store import update_alert

    for digest, record in sorted(inventory["digests"].items()):
        key = "inventory:" + hashlib.sha256(digest.encode()).hexdigest()
        update_alert(journal, key=key, code="SCAN_STALE", severity="WARNING",
                     detail=f"Running image scan is {record['scan']}; the CVE gate treats it as UNKNOWN.",
                     active=record["scan"] != "FRESH", image=digest, change_token=record["scan"])
    count = inventory["unresolved"]
    update_alert(journal, key="inventory:unresolved", code="SCAN_STALE", severity="WARNING",
                 detail=f"{count} running containers have no resolvable image digest.", active=bool(count),
                 change_token=str(count))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--context", help="kubectl context (only kind-sentinel-boutique-connected)")
    source.add_argument("--from-json", type=Path, help="Saved 'kubectl get deployments,pods -o json' output")
    parser.add_argument("--namespace", default="sentinel-boutique-connected")
    parser.add_argument("--scans", type=Path, help="Directory of scan summaries")
    parser.add_argument("--previous", type=Path, help="The last inventory, to find new digests")
    parser.add_argument("--platform-indexes", type=Path,
                        help="Recorded registry indexes with verified digest bytes; also reads Nodes to select platforms")
    parser.add_argument("--max-scan-age", type=int, default=DEFAULT_MAX_AGE, metavar="SECONDS")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scan-queue", type=Path, help="Write the digests to scan, for watch_security.py --inventory")
    parser.add_argument("--journal", type=Path, help="Dashboard SQLite journal for SCAN_STALE alerts")
    args = parser.parse_args(argv)
    if not 0 < args.max_scan_age <= 30 * 86400:
        parser.error("--max-scan-age must be 1 second to 30 days")
    objects = (json.loads(args.from_json.read_text(encoding="utf-8-sig")) if args.from_json is not None
               else read_live(args.context, args.namespace, include_nodes=args.platform_indexes is not None))
    previous = json.loads(args.previous.read_text(encoding="utf-8")) if args.previous and args.previous.is_file() else None
    inventory = build(objects, namespace=args.namespace, scans=args.scans, previous=previous,
                      max_age=args.max_scan_age, now=datetime.now(UTC),
                      platform_manifests=load_platform_manifests(args.platform_indexes) if args.platform_indexes else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    if args.scan_queue is not None and inventory["scan_queue"]:
        args.scan_queue.parent.mkdir(parents=True, exist_ok=True)
        args.scan_queue.write_text(json.dumps([{"image": digest} for digest in inventory["scan_queue"]], indent=2) + "\n",
                                   encoding="utf-8")
    if args.journal is not None:
        alerts(args.journal, inventory)
    fresh = all(record["scan"] == "FRESH" for record in inventory["digests"].values())
    print(json.dumps({"containers": len(inventory["workloads"]), "unresolved": inventory["unresolved"],
                      "digests": len(inventory["digests"]), "queued": len(inventory["scan_queue"])}))
    return 0 if fresh and not inventory["unresolved"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
