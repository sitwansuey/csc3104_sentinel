"""Read-only CVE findings and package fix recommendations from Trivy JSON.

FixedVersion describes a package version, never proof that an arbitrary image
tag fixes a CVE. No deployment or dependency mutation is performed here.
"""

from datetime import datetime, timezone
import re


DIGEST_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}\Z")
SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def image_reference(value):
    if not isinstance(value, str) or not DIGEST_IMAGE.fullmatch(value):
        raise ValueError("Scan an explicit repository@sha256:manifest-digest reference")
    return value


def _text(value, maximum=512):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid scanner field")
    return value


def summarize(report, image):
    """Reject incomplete/foreign reports rather than reporting zero findings."""
    image_reference(image)
    if (not isinstance(report, dict) or report.get("SchemaVersion") != 2
            or report.get("ArtifactType") != "container_image"
            or report.get("ArtifactName") != image
            or not isinstance(report.get("Results"), list) or not report["Results"]):
        raise ValueError("Scanner did not produce a complete report for the requested image")
    metadata = report.get("Metadata", {})
    if (not isinstance(metadata, dict) or not isinstance(metadata.get("RepoDigests"), list)
            or image not in metadata["RepoDigests"]):
        raise ValueError("Scanner evidence does not bind the requested manifest digest")
    if report.get("ExperimentalModifiedFindings"):
        raise ValueError("Suppressed or modified findings require separate review")
    findings, seen = [], set()
    for result in report["Results"]:
        if (not isinstance(result, dict) or result.get("Class") not in ("os-pkgs", "lang-pkgs")
                or not isinstance(result.get("Target"), str) or not result["Target"]):
            raise ValueError("Unsupported or incomplete scanner target")
        if result.get("Misconfigurations") or result.get("Secrets"):
            raise ValueError("Expected a vulnerability-only scan")
        vulnerabilities = result.get("Vulnerabilities", [])
        if vulnerabilities is None:
            vulnerabilities = []
        if not isinstance(vulnerabilities, list):
            raise ValueError("Invalid vulnerability results")
        for item in vulnerabilities:
            if not isinstance(item, dict):
                raise ValueError("Invalid vulnerability")
            identifier = _text(item.get("VulnerabilityID"))
            package = _text(item.get("PkgName"))
            installed = _text(item.get("InstalledVersion"))
            fixed = _text(item.get("FixedVersion", ""))
            severity = item.get("Severity")
            if not identifier or not package or severity not in SEVERITIES:
                raise ValueError("Incomplete vulnerability")
            key = (result.get("Target"), identifier, package, installed)
            if key in seen:
                continue
            seen.add(key)
            findings.append({"id": identifier, "package": package, "installed": installed,
                             "fixed": fixed or None, "severity": severity,
                             "recommendation": "Rebuild with the vendor-fixed package; test, rescan and approve a new image digest."
                                 if fixed else "No vendor fix listed; review exposure and mitigation, then rescan."})
            if len(findings) > 1000:
                raise ValueError("Scan exceeds the 1000-finding evidence limit")
    findings.sort(key=lambda item: (-SEVERITIES.index(item["severity"]), item["id"], item["package"]))
    counts = {severity: sum(item["severity"] == severity for item in findings) for severity in SEVERITIES}
    return {"image": image, "status": "FINDINGS" if findings else "NO_KNOWN_CVES",
            "scanner": "Trivy", "counts": counts, "findings": findings, "automatic_patching": False,
            "scanned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
