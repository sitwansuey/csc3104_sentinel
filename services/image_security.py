"""Read-only CVE findings and package fix recommendations from Trivy JSON.

FixedVersion describes a package version, never proof that an arbitrary image
tag fixes a CVE. No deployment or dependency mutation is performed here.
gate() below turns these summaries into PASS/FAIL/UNKNOWN results (P5.02).
"""

from datetime import datetime, timezone
import re

from shared.canonical import sha256_hex


DIGEST_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}\Z")
SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def image_reference(value):
    if not isinstance(value, str) or not DIGEST_IMAGE.fullmatch(value) or "://" in value:
        raise ValueError("Scan an explicit repository@sha256:manifest-digest reference")
    return value


def _canonical_digest(reference):
    if not isinstance(reference, str) or "@" not in reference:
        return None
    repository, digest = reference.rsplit("@", 1)
    for prefix in ("docker.io/", "index.docker.io/"):
        if repository.startswith(prefix):
            repository = repository[len(prefix):]
            break
    first = repository.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return repository + "@" + digest
    return "docker.io/" + (repository if "/" in repository else "library/" + repository) + "@" + digest


def _text(value, maximum=512):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid scanner field")
    return value


def summarize(report, image, *, local=False):
    """Reject incomplete/foreign reports rather than reporting zero findings."""
    if local:
        if not isinstance(image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Local scans require an immutable Docker image configuration ID")
    else:
        image_reference(image)
    if (not isinstance(report, dict) or report.get("SchemaVersion") != 2
            or report.get("ArtifactType") != "container_image"
            or report.get("ArtifactName") != ("/report/image.tar" if local else image)
            or not isinstance(report.get("Results"), list) or not report["Results"]):
        raise ValueError("Scanner did not produce a complete report for the requested image")
    metadata = report.get("Metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Missing scanner image metadata")
    bound = (metadata.get("ImageID") == image if local else
             isinstance(metadata.get("RepoDigests"), list) and _canonical_digest(image) in
             [_canonical_digest(value) for value in metadata["RepoDigests"]])
    if not bound:
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
    return {"image": "local://" + image if local else image, "scan_source": "local_archive" if local else "remote_manifest",
            "status": "FINDINGS" if findings else "NO_KNOWN_CVES",
            "scanner": "Trivy", "counts": counts, "findings": findings, "automatic_patching": False,
            "scanned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


# The CVE gate (P5.02). One pure function shared by the Guard (through
# live_state.image_scans), the admission parameters and CI
# (scripts/security/gate_images.py). It reads only the summaries above, a
# reviewed rule block and reviewed exceptions. It never scans, so a scanner
# database update cannot change a decision already bound to its inputs.

GATE_FIELDS = frozenset({"block_severities", "mode", "max_scan_age_seconds", "trusted_scanner_images",
                         "unknown_result", "exceptions_sha256"})
GATE_REQUIRED = frozenset({"block_severities", "mode", "max_scan_age_seconds", "trusted_scanner_images"})
GATE_MODES = ("non_regression", "strict")
MAX_SCAN_AGE_SECONDS = 30 * 86400
MAX_EXCEPTION_SECONDS = 30 * 86400
MAX_GATE_SCANS = 64
MAX_EXCEPTIONS = 256
EXCEPTION_FIELDS = frozenset({"image", "id", "approved_at", "expires_at", "rationale", "reviewer"})
_ROLE = re.compile(r"[a-z][a-z0-9-]{1,63}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PINNED_ONLY = re.compile(r"(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}\Z")
_LIST_LIMIT = 20


def canonical_image(reference):
    """repository@sha256:<hex> with Docker Hub names expanded and any tag dropped; None if not digest-pinned.

    "redis@sha256:x", "docker.io/library/redis@sha256:x" and
    "redis:7@sha256:x" name one manifest, so they share one scan.
    """
    if not isinstance(reference, str) or not DIGEST_IMAGE.fullmatch(reference) or "://" in reference:
        return None
    repository, digest = reference.rsplit("@", 1)
    head, slash, last = repository.rpartition("/")
    # A tag before the digest; a registry port is never in the last path segment.
    repository = head + slash + last.split(":", 1)[0]
    return _canonical_digest(repository + "@" + digest)


def pinned_reference(reference):
    """reference unchanged if it is repository@sha256:<hex> with no tag; ValueError otherwise."""
    if not isinstance(reference, str) or not _PINNED_ONLY.fullmatch(reference):
        raise ValueError("Expected repository@sha256:<64 hex> with no tag")
    return reference


def _utc(value, name):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{name}: expected a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name}: expected a UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name}: expected a UTC timestamp")
    return parsed


def validate_gate_rules(rules):
    """The reviewed image_vulnerability_gate block; ValueError on an unknown, missing or weakened field."""
    if not isinstance(rules, dict):
        raise ValueError("image_vulnerability_gate: expected an object")
    if rules.keys() - GATE_FIELDS:
        raise ValueError("image_vulnerability_gate: unexpected fields")
    if GATE_REQUIRED - rules.keys():
        raise ValueError("image_vulnerability_gate: missing required fields")
    severities = rules["block_severities"]
    if (not isinstance(severities, list) or not severities or any(item not in SEVERITIES for item in severities)
            or len(set(severities)) != len(severities) or "CRITICAL" not in severities):
        raise ValueError("image_vulnerability_gate.block_severities: distinct severities that include CRITICAL")
    if rules["mode"] not in GATE_MODES:
        raise ValueError("image_vulnerability_gate.mode: expected non_regression or strict")
    age = rules["max_scan_age_seconds"]
    if type(age) is not int or not 0 < age <= MAX_SCAN_AGE_SECONDS:
        raise ValueError("image_vulnerability_gate.max_scan_age_seconds: expected 1 second to 30 days")
    scanners = rules["trusted_scanner_images"]
    if (not isinstance(scanners, list) or not scanners or any(canonical_image(item) is None for item in scanners)
            or len({canonical_image(item) for item in scanners}) != len(scanners)):
        raise ValueError("image_vulnerability_gate.trusted_scanner_images: distinct digest-pinned scanner images")
    if rules.get("unknown_result", "ESCALATE") not in ("ESCALATE", "REJECT"):
        raise ValueError("image_vulnerability_gate.unknown_result: expected ESCALATE or REJECT")
    if "exceptions_sha256" in rules and (not isinstance(rules["exceptions_sha256"], str)
                                         or not _HEX64.fullmatch(rules["exceptions_sha256"])):
        raise ValueError("image_vulnerability_gate.exceptions_sha256: expected 64 lowercase hex characters")
    return rules


def validate_exceptions(exceptions):
    """Reviewed, time-bound exceptions: a digest, a CVE id, a reviewer role (not a person), a rationale, at most 30 days."""
    if not isinstance(exceptions, list) or len(exceptions) > MAX_EXCEPTIONS:
        raise ValueError("exceptions: expected a list of at most 256 entries")
    seen = set()
    for index, item in enumerate(exceptions):
        name = f"exceptions[{index}]"
        if not isinstance(item, dict) or set(item) != EXCEPTION_FIELDS:
            raise ValueError(f"{name}: expected exactly the fields {sorted(EXCEPTION_FIELDS)}")
        image = canonical_image(item["image"])
        if image is None:
            raise ValueError(f"{name}.image: expected a digest-pinned image")
        if not isinstance(item["id"], str) or not item["id"].strip() or len(item["id"]) > 128 or any(
                ord(character) < 32 for character in item["id"]):
            raise ValueError(f"{name}.id: expected a vulnerability id")
        if not isinstance(item["rationale"], str) or not item["rationale"].strip() or len(item["rationale"]) > 2000:
            raise ValueError(f"{name}.rationale: expected a reviewed rationale")
        if not isinstance(item["reviewer"], str) or not _ROLE.fullmatch(item["reviewer"]):
            raise ValueError(f"{name}.reviewer: expected a reviewer role such as security-owner")
        approved = _utc(item["approved_at"], f"{name}.approved_at")
        lifetime = (_utc(item["expires_at"], f"{name}.expires_at") - approved).total_seconds()
        if not 0 < lifetime <= MAX_EXCEPTION_SECONDS:
            raise ValueError(f"{name}: an exception must last more than 0 seconds and at most 30 days")
        key = (image, item["id"])
        if key in seen:
            raise ValueError(f"{name}: duplicate exception")
        seen.add(key)
    return exceptions


def expired_exceptions(exceptions, now):
    """(image, id) of each exception expired at now. CI fails on any."""
    return sorted((item["image"], item["id"]) for item in validate_exceptions(exceptions)
                  if now >= _utc(item["expires_at"], "expires_at"))


def _scan_record(scan, index):
    name = f"image_scans.scans[{index}]"
    if not isinstance(scan, dict):
        raise ValueError(f"{name}: expected a scan summary")
    image = canonical_image(scan.get("image"))
    if image is None:
        raise ValueError(f"{name}.image: expected a digest-pinned image")
    status = scan.get("status")
    if status not in ("FINDINGS", "NO_KNOWN_CVES", "UNKNOWN"):
        raise ValueError(f"{name}.status: expected FINDINGS, NO_KNOWN_CVES or UNKNOWN")
    findings = scan.get("findings", [])
    if not isinstance(findings, list) or len(findings) > 1000:
        raise ValueError(f"{name}.findings: expected at most 1000 findings")
    ids = {}
    for item in findings:
        if (not isinstance(item, dict) or item.get("severity") not in SEVERITIES or not isinstance(item.get("id"), str)
                or not item["id"].strip() or len(item["id"]) > 128):
            raise ValueError(f"{name}.findings: expected findings with an id and a severity")
        previous = ids.get(item["id"])
        if previous is None or SEVERITIES.index(item["severity"]) > SEVERITIES.index(previous):
            ids[item["id"]] = item["severity"]
    if status == "UNKNOWN":
        return image, {"status": status, "ids": {}, "scanner": None, "scanned_at": None}
    if (status == "FINDINGS") != bool(findings):
        raise ValueError(f"{name}.status: does not match its findings")
    return image, {"status": status, "ids": ids, "scanner": canonical_image(scan.get("scanner_image")),
                   "scanned_at": _utc(scan.get("scanned_at"), f"{name}.scanned_at")}


def validate_image_scans(value):
    """({canonical image: scan}, exceptions) from live_state.image_scans; ValueError if malformed."""
    if not isinstance(value, dict) or "scans" not in value or set(value) - {"scans", "exceptions"}:
        raise ValueError("image_scans: expected {scans, exceptions}")
    scans = value["scans"]
    if not isinstance(scans, list) or len(scans) > MAX_GATE_SCANS:
        raise ValueError("image_scans.scans: expected at most 64 scan summaries")
    parsed = {}
    for index, scan in enumerate(scans):
        image, record = _scan_record(scan, index)
        if image in parsed:
            raise ValueError("image_scans.scans: expected one scan per image digest")
        parsed[image] = record
    return parsed, validate_exceptions(value.get("exceptions", []))


def _gate_result(code, status, container, image, message, ids=()):
    return {"code": code, "status": status, "container": container, "image": image,
            "ids": sorted(ids)[:_LIST_LIMIT], "id_count": len(ids), "message": message}


def gate(image_scans, candidate_images, current_images, rules, now):
    """Classify every candidate image against trusted scans. Pure: no I/O.

    candidate_images maps each container name to the image an action would
    run. current_images maps the same names to the images in the template now,
    or is None when that template cannot be read. A result is PASS, FAIL or
    UNKNOWN:

    - IMAGE_SCAN_MISSING / IMAGE_SCAN_STALE (UNKNOWN): no completed scan for the
      digest (a failed scan is never clean), or one older than
      max_scan_age_seconds or dated in the future.
    - IMAGE_SCANNER_UNTRUSTED (FAIL): the scan names no reviewed scanner image.
    - IMAGE_CVE_REGRESSION (FAIL): blocked-severity ids that the same
      container's current image does not have.
    - IMAGE_CVE_INHERITED: blocked ids the current image already has. PASS in
      non_regression mode (a config-only rollback keeps its digest), FAIL in
      strict mode.
    - IMAGE_CURRENT_SCAN_UNAVAILABLE (UNKNOWN): blocked ids that cannot be shown
      to be inherited, because the current image has no usable scan.
    - IMAGE_CVE_BLOCKED (FAIL): strict mode, blocked ids, current image unknown.
    - IMAGE_CVE_EXCEPTED (PASS) / IMAGE_EXCEPTION_EXPIRED (FAIL): a reviewed
      exception for that digest and id that is still valid / has expired.
    - IMAGE_EXCEPTIONS_UNREVIEWED (FAIL): the exceptions differ from the
      reviewed exceptions_sha256, so none of them apply.
    - IMAGE_CVE_CLEAR (PASS): a usable scan with no blocked-severity id.

    With unknown_result REJECT every UNKNOWN is a FAIL instead.
    """
    validate_gate_rules(rules)
    if not isinstance(candidate_images, dict) or not (current_images is None or isinstance(current_images, dict)):
        raise ValueError("Candidate and current images map container names to images")
    scans, exceptions = validate_image_scans(image_scans)
    blocked_levels = set(rules["block_severities"])
    trusted = {canonical_image(item) for item in rules["trusted_scanner_images"]}
    strict = rules["mode"] == "strict"
    unknown = "FAIL" if rules.get("unknown_result", "ESCALATE") == "REJECT" else "UNKNOWN"
    results = []
    if exceptions and sha256_hex(exceptions) != rules.get("exceptions_sha256"):
        results.append(_gate_result("IMAGE_EXCEPTIONS_UNREVIEWED", "FAIL", None, None,
                                    "The exceptions differ from the reviewed exceptions_sha256; none apply."))
        exceptions = []
    valid, expired = set(), set()
    for item in exceptions:
        key = (canonical_image(item["image"]), item["id"])
        if now >= _utc(item["expires_at"], "expires_at"):
            expired.add(key)
        elif _utc(item["approved_at"], "approved_at") <= now:
            valid.add(key)

    def usable(image):
        """(scan, None) for a usable scan of image, otherwise (None, (code, status, message))."""
        if image is None:
            return None, ("IMAGE_SCAN_MISSING", unknown, "The image is not pinned by digest, so no scan binds to it.")
        record = scans.get(image)
        if record is None or record["status"] == "UNKNOWN":
            return None, ("IMAGE_SCAN_MISSING", unknown, "No completed scan exists for this digest; a failed scan is never clean.")
        if record["scanner"] not in trusted:
            return None, ("IMAGE_SCANNER_UNTRUSTED", "FAIL", "The scan does not name a reviewed, digest-pinned scanner.")
        age = (now - record["scanned_at"]).total_seconds()
        if not 0 <= age <= rules["max_scan_age_seconds"]:
            return None, ("IMAGE_SCAN_STALE", unknown, "The scan is older than max_scan_age_seconds or dated in the future.")
        return record, None

    for container in sorted(candidate_images):
        image = canonical_image(candidate_images[container])
        scan, problem = usable(image)
        if problem is not None:
            results.append(_gate_result(problem[0], problem[1], container, image or "unpinned", problem[2]))
            continue
        blocked = {cve for cve, severity in scan["ids"].items() if severity in blocked_levels}
        if not blocked:
            results.append(_gate_result("IMAGE_CVE_CLEAR", "PASS", container, image, "No finding at a blocked severity."))
            continue
        excepted = {cve for cve in blocked if (image, cve) in valid}
        remaining = blocked - excepted
        current, current_problem = None, ("IMAGE_CURRENT_SCAN_UNAVAILABLE", unknown, "The current template cannot be read.")
        if current_images is not None and container not in current_images:
            current_problem = ("IMAGE_CURRENT_SCAN_UNAVAILABLE", unknown, "The current template has no container of this name.")
        elif current_images is not None:
            current, found = usable(canonical_image(current_images[container]))
            if found is not None:
                current_problem = ("IMAGE_SCANNER_UNTRUSTED" if found[1] == "FAIL" else "IMAGE_CURRENT_SCAN_UNAVAILABLE",
                                   found[1], "The current image has no usable scan, so inheritance cannot be shown. " + found[2])
        inherited = remaining & set(current["ids"]) if current is not None else set()
        regression = remaining - inherited if current is not None else set()
        lapsed = {cve for cve in (remaining if strict else regression) if (image, cve) in expired}
        if excepted:
            results.append(_gate_result("IMAGE_CVE_EXCEPTED", "PASS", container, image,
                                        "Covered by a reviewed, unexpired exception for this digest.", excepted))
        if lapsed:
            results.append(_gate_result("IMAGE_EXCEPTION_EXPIRED", "FAIL", container, image,
                                        "The exception that covered these findings has expired.", lapsed))
        if current is None and remaining - lapsed:
            if strict:
                results.append(_gate_result("IMAGE_CVE_BLOCKED", "FAIL", container, image,
                                            "Strict mode blocks these findings without a valid exception.", remaining - lapsed))
            else:
                results.append(_gate_result(*current_problem[:2], container, image, current_problem[2], remaining))
        if regression - lapsed:
            results.append(_gate_result("IMAGE_CVE_REGRESSION", "FAIL", container, image,
                                        "The current image does not have these blocked findings; the action would add them.",
                                        regression - lapsed))
        if inherited - lapsed:
            results.append(_gate_result("IMAGE_CVE_INHERITED", "FAIL" if strict else "PASS", container, image,
                                        "Strict mode blocks inherited findings without a valid exception." if strict else
                                        "The current image already has these findings; non-regression mode allows them.",
                                        inherited - lapsed))
    return results
