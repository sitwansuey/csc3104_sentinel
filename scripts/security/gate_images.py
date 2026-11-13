"""Run the CVE gate (services/image_security.gate) outside the Guard: CI and admission parameters.

Subcommands:

  gate              Gate candidate images against current images, as the Guard does.
                    Exit 0 PASS, 1 FAIL, 2 UNKNOWN.
  vap-params        Write the ConfigMap that deploy/security/approved-images-vap.yaml
                    reads: the digests of an inventory (image_inventory.py) and of
                    reviewed baselines that pass the gate. Digests only, never tags.
                    Exit 0 when every inventory image passed, 2 otherwise.
  check-exceptions  Validate a reviewed exceptions file. Exit 1 if any exception is
                    malformed or expired (CI runs this).

The rules file is a gate block (deploy/security/cve-gate.json) or a Guard
policy holding security_constraints.image_vulnerability_gate. Scans are read
from a directory of scan summaries (scan_image_security.py, watch_security.py).
Nothing here scans, calls a cluster or changes one.
"""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.image_scans import load_cve_exceptions, read_image_scans
from services.image_security import (canonical_image, expired_exceptions, gate, pinned_reference,
                                     validate_exceptions, validate_gate_rules)

CONFIGMAP_NAME = "sentinel-approved-images"
CONFIGMAP_NAMESPACE = "sentinel-admission"
EXIT = {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise argparse.ArgumentTypeError("expected a UTC timestamp")
    return parsed


def load_rules(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and "security_constraints" in data:
        data = data["security_constraints"].get("image_vulnerability_gate") if isinstance(
            data["security_constraints"], dict) else None
    return validate_gate_rules(data)


def verdict(results):
    statuses = {item["status"] for item in results}
    return "FAIL" if "FAIL" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "PASS"


def pairs(values, name):
    images = {}
    for value in values or []:
        container, separator, image = value.partition("=")
        if not separator or not container or not image or container in images:
            raise ValueError(f"{name}: expected distinct CONTAINER=IMAGE pairs")
        images[container] = image
    return images


def run_gate(*, scans, rules, exceptions, candidate, current, now):
    """The gate report for one candidate set: {verdict, results}."""
    image_scans = read_image_scans(scans, [*candidate.values(), *(current or {}).values()], exceptions)
    results = gate(image_scans, candidate, current, rules, now)
    return {"verdict": verdict(results), "mode": rules["mode"], "results": results}


def _inventory_workloads(inventory):
    if not isinstance(inventory, dict) or inventory.get("schema") != "sentinel-image-inventory-v1" or not isinstance(
            inventory.get("workloads"), list):
        raise ValueError("Expected a sentinel-image-inventory-v1 file")
    current = {}
    for item in inventory["workloads"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("container"), str):
            raise ValueError("Malformed inventory workload")
        current.setdefault((item.get("kind"), item["name"]), {})[item["container"]] = item.get("digest")
    return current


def _baseline_images(path):
    baseline = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    name = baseline.get("target", {}).get("name") if isinstance(baseline, dict) else None
    spec = baseline.get("pod_template", {}).get("spec", {}) if isinstance(baseline, dict) else {}
    images = {}
    for group in ("containers", "initContainers"):
        for container in spec.get(group, []) if isinstance(spec, dict) else []:
            if isinstance(container, dict) and isinstance(container.get("name"), str):
                images[container["name"]] = container.get("image")
    if not isinstance(name, str) or not images:
        raise ValueError(f"{Path(path).name}: expected a baseline with a target and a Pod template")
    return name, images


def vap_params(*, scans, rules, exceptions, inventory, baselines=None, extra_images=(), now):
    """(ConfigMap, report). The ConfigMap lists approved repository@sha256 references only.

    A running image is approved when its own gate passes with itself as the
    current image (in non_regression mode its findings are inherited; it still
    needs a trusted, fresh scan). A baseline image is approved when it passes
    against the running images of the same Deployment. extra_images (probe or
    Sentinel images, gated elsewhere) are listed separately in the report.
    """
    current = _inventory_workloads(inventory)
    approved, report = set(), {"workloads": [], "baselines": [], "extra_images": []}
    for (kind, name), containers in sorted(current.items(), key=lambda item: (str(item[0][0]), item[0][1])):
        resolved = {container: digest for container, digest in containers.items() if digest is not None}
        result = run_gate(scans=scans, rules=rules, exceptions=exceptions, candidate=resolved, current=resolved, now=now)
        for container in sorted(containers):
            outcome = ("UNKNOWN" if containers[container] is None else
                       verdict([item for item in result["results"] if item["container"] in (container, None)]))
            if outcome == "PASS":
                approved.add(pinned_reference(canonical_image(containers[container])))
            report["workloads"].append({"kind": kind, "name": name, "container": container,
                                        "image": containers[container], "verdict": outcome})
    for path in sorted(Path(baselines).glob("*.json")) if baselines is not None else []:
        name, images = _baseline_images(path)
        running = current.get(("Deployment", name))
        result = run_gate(scans=scans, rules=rules, exceptions=exceptions, candidate=images,
                          current=running, now=now)
        for container in sorted(images):
            outcome = verdict([item for item in result["results"] if item["container"] in (container, None)])
            image = canonical_image(images[container])
            if outcome == "PASS" and image is not None:
                approved.add(pinned_reference(image))
            report["baselines"].append({"name": name, "container": container, "image": images[container],
                                        "verdict": outcome if image is not None else "UNKNOWN"})
    for image in extra_images:
        approved.add(pinned_reference(image))
        report["extra_images"].append(image)
    # Admission compares the exact string, and a Pod spec may use a short Docker Hub name for the same digest.
    for image in list(approved):
        if image.startswith("docker.io/"):
            rest = image[len("docker.io/"):]
            approved.add(pinned_reference(rest))
            if rest.startswith("library/"):
                approved.add(pinned_reference(rest[len("library/"):]))
    configmap = {"apiVersion": "v1", "kind": "ConfigMap",
                 "metadata": {"name": CONFIGMAP_NAME, "namespace": CONFIGMAP_NAMESPACE,
                              "labels": {"app.kubernetes.io/managed-by": "sentinel-gate-images"}},
                 "data": {"digests": "\n".join(sorted(approved))}}
    report["approved"] = sorted(approved)
    report["verdict"] = "PASS" if all(item["verdict"] == "PASS" for item in report["workloads"]) else "UNKNOWN"
    return configmap, report


def check_exceptions(path, now):
    """(ok, report) for a reviewed exceptions file: valid, and none expired."""
    try:
        exceptions = validate_exceptions(load_cve_exceptions(path))
    except (OSError, ValueError) as exc:
        return False, {"status": "INVALID", "detail": str(exc)[:300]}
    expired = expired_exceptions(exceptions, now)
    return not expired, {"status": "EXPIRED" if expired else "OK", "count": len(exceptions),
                         "expired": [{"image": image, "id": cve} for image, cve in expired]}


def _parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--now", type=utc, help="Evaluation time (UTC); defaults to now")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("gate", "vap-params"):
        command = commands.add_parser(name)
        command.add_argument("--scans", required=True, type=Path, help="Directory of scan summaries")
        command.add_argument("--rules", required=True, type=Path, help="Gate block or Guard policy")
        command.add_argument("--exceptions", type=Path, help="Reviewed exceptions file")
        command.add_argument("--output", type=Path, help="Write the report (gate) or the ConfigMap (vap-params) here")
    gate_command = commands.choices["gate"]
    gate_command.add_argument("--candidate", action="append", required=True, metavar="CONTAINER=IMAGE")
    gate_command.add_argument("--current", action="append", metavar="CONTAINER=IMAGE",
                              help="Images running now; omit when unknown")
    params = commands.choices["vap-params"]
    params.add_argument("--inventory", required=True, type=Path, help="image_inventory.py output")
    params.add_argument("--baselines", type=Path, help="Directory of reviewed baselines (<name>.json)")
    params.add_argument("--extra-image", action="append", default=[], metavar="REPOSITORY@SHA256",
                        help="An image gated elsewhere (probe or Sentinel image); digests only")
    params.add_argument("--report", type=Path, help="Write the per-image report here")
    exceptions = commands.add_parser("check-exceptions")
    exceptions.add_argument("path", type=Path)
    return parser


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    args = _parser().parse_args(argv)
    now = args.now or datetime.now(UTC)
    if args.command == "check-exceptions":
        ok, report = check_exceptions(args.path, now)
        print(json.dumps(report))
        return 0 if ok else 1
    rules = load_rules(args.rules)
    if args.command == "gate":
        report = run_gate(scans=args.scans, rules=rules, exceptions=args.exceptions,
                          candidate=pairs(args.candidate, "--candidate"),
                          current=pairs(args.current, "--current") if args.current else None, now=now)
        if args.output:
            _write(args.output, report)
        print(json.dumps({"verdict": report["verdict"], "codes": sorted({item["code"] for item in report["results"]})}))
        return EXIT[report["verdict"]]
    inventory = json.loads(args.inventory.read_text(encoding="utf-8-sig"))
    configmap, report = vap_params(scans=args.scans, rules=rules, exceptions=args.exceptions, inventory=inventory,
                                   baselines=args.baselines, extra_images=args.extra_image, now=now)
    if args.output:
        _write(args.output, configmap)
    if args.report:
        _write(args.report, report)
    print(json.dumps({"verdict": report["verdict"], "approved": len(report["approved"])}))
    return EXIT[report["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
