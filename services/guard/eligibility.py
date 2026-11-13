"""Pure rollback eligibility and security checks for trusted Guard snapshots.

Health evidence and reference inventories must come from trusted stores/adapters,
never from the recovery planner. Reference checks compare metadata only: this
module never needs ConfigMap contents or Secret values. CSI, PVC, external volume
types and unknown projected/environment reference forms cannot be certified by
this small compatibility checker and produce UNKNOWN instead of a safe result.
"""

import re

from services import image_security
from services.guard.common import GuardInputError, finding, mapping, string, utc_timestamp


_BASELINE_FIELDS = {
    "max_age_seconds", "min_healthy_duration_seconds", "trusted_sources",
    "require_health_evidence", "require_compatible_references", "require_image_digests",
}
_SECURITY_FIELDS = {
    "deny_privileged", "deny_host_access", "allowed_image_registries",
    "allowed_service_accounts", "forbid_service_account_change", "image_vulnerability_gate",
}
_DIGEST_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}")
_REGISTRY = re.compile(r"(?:localhost|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::[0-9]+)?")
_REFERENCE_FIELDS = {"kind", "namespace", "name", "uid", "resource_version"}


def validate_policy(policy):
    """Reject misspelled/ambiguous configured rules; absent blocks stay optional."""
    for block, fields in (("baseline_constraints", _BASELINE_FIELDS),
                          ("security_constraints", _SECURITY_FIELDS)):
        if block not in policy:
            continue
        value = mapping(policy[block], f"policy.{block}")
        if value.keys() - fields:
            raise GuardInputError(f"policy.{block}: unexpected fields")
        for key, item in value.items():
            path = f"policy.{block}.{key}"
            if key == "image_vulnerability_gate":
                try:
                    image_security.validate_gate_rules(item)
                except ValueError as exc:
                    raise GuardInputError(f"policy.{block}.{exc}") from exc
            elif key in {"max_age_seconds", "min_healthy_duration_seconds"}:
                if type(item) is not int or item < 0:
                    raise GuardInputError(f"{path}: expected a nonnegative integer")
            elif key in {"trusted_sources", "allowed_image_registries", "allowed_service_accounts"}:
                if not isinstance(item, list) or (key == "trusted_sources" and not item):
                    raise GuardInputError(f"{path}: expected a list of nonempty strings")
                for index, entry in enumerate(item):
                    string(entry, f"{path}[{index}]")
                    if key == "allowed_image_registries" and not _REGISTRY.fullmatch(entry):
                        raise GuardInputError(f"{path}[{index}]: expected a lowercase registry host, optionally with port")
                if len(set(item)) != len(item):
                    raise GuardInputError(f"{path}: duplicate entries")
            elif type(item) is not bool:
                raise GuardInputError(f"{path}: expected a boolean")


def _result(code, path, actual, expected, message, status="FAIL"):
    return finding(code, path, actual, expected, message, status=status)


def _invalid(path, message):
    # Never include arbitrary malformed objects: these could contain credentials.
    return _result("BASELINE_ELIGIBILITY_INVALID", path, "malformed", "valid typed evidence", message)


def _containers(spec):
    containers = []
    for group in ("containers", "initContainers"):
        values = spec.get(group, [] if group == "initContainers" else None)
        if not isinstance(values, list) or (group == "containers" and not values):
            raise GuardInputError(f"baseline.pod_template.spec.{group}: expected a nonempty container list" if group == "containers" else f"baseline.pod_template.spec.{group}: expected a list")
        for index, value in enumerate(values):
            path = f"baseline.pod_template.spec.{group}[{index}]"
            mapping(value, path)
            string(value.get("name"), f"{path}.name")
            containers.append((value, path))
    return containers


def _health(baseline, proposal, rules, checked_at):
    output = []
    healthy_at = None
    if "max_age_seconds" in rules:
        try:
            healthy_at = utc_timestamp(baseline.get("healthy_observed_at"), "baseline.healthy_observed_at")
        except GuardInputError:
            output.append(_invalid("baseline.healthy_observed_at", "Baseline health time must be a UTC timestamp."))
        else:
            age = (checked_at - healthy_at).total_seconds()
            output.append(_result("BASELINE_AGE", "baseline.healthy_observed_at", age,
                                  {"min_age_seconds": 0, "max_age_seconds": rules["max_age_seconds"]},
                                  "Baseline health evidence must be recent and not in the future.",
                                  "PASS" if 0 <= age <= rules["max_age_seconds"] else "FAIL"))
    evidence = baseline.get("health_evidence")
    needed = rules.get("require_health_evidence", False) or "min_healthy_duration_seconds" in rules or "trusted_sources" in rules
    if evidence is None:
        if needed:
            output.append(_result("BASELINE_HEALTH_EVIDENCE_MISSING", "baseline.health_evidence", None,
                                  "trusted evidence bound to target and template", "Health evidence is unavailable.", "UNKNOWN"))
        return output
    try:
        mapping(evidence, "baseline.health_evidence")
        for key in ("source", "evidence_id", "status", "template_sha256"):
            string(evidence.get(key), f"baseline.health_evidence.{key}")
        mapping(evidence.get("target"), "baseline.health_evidence.target")
        start = utc_timestamp(evidence.get("window_start"), "baseline.health_evidence.window_start")
        end = utc_timestamp(evidence.get("window_end"), "baseline.health_evidence.window_end")
        if healthy_at is None:
            healthy_at = utc_timestamp(baseline.get("healthy_observed_at"), "baseline.healthy_observed_at")
    except GuardInputError as exc:
        output.append(_invalid("baseline.health_evidence", str(exc)))
        return output
    checks = (
        ("BASELINE_HEALTH_STATUS", "status", evidence["status"], "HEALTHY", evidence["status"] == "HEALTHY", "Baseline must have a healthy verification result."),
        ("BASELINE_HEALTH_TARGET", "target", evidence["target"], proposal["target"], evidence["target"] == proposal["target"], "Health evidence must identify the exact recovery target."),
        ("BASELINE_HEALTH_TEMPLATE", "template_sha256", evidence["template_sha256"], baseline.get("template_sha256"), evidence["template_sha256"] == baseline.get("template_sha256"), "Health evidence must identify the exact saved template."),
        ("BASELINE_HEALTH_WINDOW", "window_end", evidence["window_end"], "ordered, nonfuture, ending at healthy_observed_at", start <= end <= checked_at and end == healthy_at, "Health window must be ordered, nonfuture and consistent with the saved baseline."),
    )
    for code, key, actual, expected, okay, message in checks:
        # Identity is expected to be metadata; constrain the result to the known
        # identity fields rather than echoing arbitrary evidence object fields.
        if key == "target":
            actual = {name: evidence["target"].get(name) if isinstance(evidence["target"].get(name), str) else "malformed" for name in proposal["target"]}
        output.append(_result(code, f"baseline.health_evidence.{key}", actual, expected, message, "PASS" if okay else "FAIL"))
    if "trusted_sources" in rules:
        output.append(_result("BASELINE_HEALTH_SOURCE", "baseline.health_evidence.source", evidence["source"],
                              rules["trusted_sources"], "Health evidence must originate from a configured verifier.",
                              "PASS" if evidence["source"] in rules["trusted_sources"] else "FAIL"))
    if "min_healthy_duration_seconds" in rules:
        duration = (end - start).total_seconds()
        output.append(_result("BASELINE_HEALTH_DURATION", "baseline.health_evidence.window_start", duration,
                              {"min_seconds": rules["min_healthy_duration_seconds"]}, "Health verification must cover the configured stability window.",
                              "PASS" if duration >= rules["min_healthy_duration_seconds"] else "FAIL"))
    return output


def _reference_inventory(value, path):
    if value is None:
        return None
    if not isinstance(value, list):
        raise GuardInputError(f"{path}: expected a metadata-only reference list")
    result = {}
    for index, record in enumerate(value):
        entry_path = f"{path}[{index}]"
        mapping(record, entry_path)
        if set(record) != _REFERENCE_FIELDS or record.get("kind") not in {"ConfigMap", "Secret"}:
            raise GuardInputError(f"{entry_path}: expected ConfigMap/Secret identity and version metadata only")
        for key in _REFERENCE_FIELDS:
            string(record[key], f"{entry_path}.{key}")
        identity = (record["kind"], record["namespace"], record["name"])
        if identity in result:
            raise GuardInputError(f"{entry_path}: duplicate reference identity")
        result[identity] = (record["uid"], record["resource_version"])
    return result


def _references(spec, containers, namespace):
    """Return referenced ConfigMaps/Secrets plus unsupported-source findings."""
    found, unsupported = set(), []

    def add(value, kind, path):
        mapping(value, path)
        string(value.get("name"), f"{path}.name")
        found.add((kind, namespace, value["name"]))

    def unknown(path):
        unsupported.append(_result("BASELINE_REFERENCE_UNSUPPORTED", path, "unsupported reference form",
                                   "ConfigMap/Secret reference with trusted metadata", "This external reference cannot be verified by the configured compatibility checker.", "UNKNOWN"))

    for container, path in containers:
        env = container.get("env", [])
        sources = container.get("envFrom", [])
        if not isinstance(env, list) or not isinstance(sources, list):
            raise GuardInputError(f"{path}: env and envFrom must be lists")
        for index, entry in enumerate(env):
            entry_path = f"{path}.env[{index}]"
            mapping(entry, entry_path)
            if "valueFrom" not in entry:
                continue
            source = mapping(entry["valueFrom"], f"{entry_path}.valueFrom")
            if len(source) != 1:
                raise GuardInputError(f"{entry_path}.valueFrom: expected exactly one source")
            for key, value in source.items():
                ref_path = f"{entry_path}.valueFrom.{key}"
                if key in {"configMapKeyRef", "secretKeyRef"}:
                    add(value, "ConfigMap" if key == "configMapKeyRef" else "Secret", ref_path)
                    string(value.get("key"), f"{ref_path}.key")
                elif key not in {"fieldRef", "resourceFieldRef"}:
                    unknown(ref_path)
        for index, entry in enumerate(sources):
            entry_path = f"{path}.envFrom[{index}]"
            mapping(entry, entry_path)
            keys = set(entry) - {"prefix"}
            if len(keys) != 1:
                raise GuardInputError(f"{entry_path}: expected exactly one reference")
            for key in keys:
                if key in {"configMapRef", "secretRef"}:
                    add(entry[key], "ConfigMap" if key == "configMapRef" else "Secret", f"{entry_path}.{key}")
                else:
                    unknown(f"{entry_path}.{key}")
    volumes = spec.get("volumes", [])
    if not isinstance(volumes, list):
        raise GuardInputError("baseline.pod_template.spec.volumes: expected a list")
    for index, volume in enumerate(volumes):
        path = f"baseline.pod_template.spec.volumes[{index}]"
        mapping(volume, path)
        types = set(volume) - {"name"}
        if len(types) != 1:
            raise GuardInputError(f"{path}: expected one volume source")
        kind = next(iter(types))
        if kind == "secret":
            source = mapping(volume[kind], f"{path}.secret")
            add({"name": source.get("secretName")}, "Secret", f"{path}.secret")
        elif kind == "configMap":
            add(volume[kind], "ConfigMap", f"{path}.configMap")
        elif kind == "projected":
            projected = mapping(volume[kind], f"{path}.projected")
            sources = projected.get("sources")
            if not isinstance(sources, list) or not sources:
                raise GuardInputError(f"{path}.projected.sources: expected a nonempty list")
            for offset, source in enumerate(sources):
                source_path = f"{path}.projected.sources[{offset}]"
                mapping(source, source_path)
                if len(source) != 1:
                    raise GuardInputError(f"{source_path}: expected exactly one projected source")
                key = next(iter(source))
                if key in {"secret", "configMap"}:
                    add(source[key], "Secret" if key == "secret" else "ConfigMap", f"{source_path}.{key}")
                elif key not in {"downwardAPI", "serviceAccountToken"}:
                    unknown(f"{source_path}.{key}")
        elif kind not in {"emptyDir", "downwardAPI"}:
            unknown(f"{path}.{kind}")
    pull_secrets = spec.get("imagePullSecrets", [])
    if not isinstance(pull_secrets, list):
        raise GuardInputError("baseline.pod_template.spec.imagePullSecrets: expected a list")
    for index, entry in enumerate(pull_secrets):
        add(entry, "Secret", f"baseline.pod_template.spec.imagePullSecrets[{index}]")
    return found, unsupported


def _compatible_references(baseline, live_state, proposal, spec, containers):
    try:
        refs, output = _references(spec, containers, proposal["target"]["namespace"])
        saved = _reference_inventory(baseline.get("reference_versions"), "baseline.reference_versions")
        current = _reference_inventory(live_state.get("reference_versions"), "live_state.reference_versions")
    except GuardInputError as exc:
        return [_invalid("baseline.reference_versions", str(exc))]
    if not refs:
        output.append(_result("BASELINE_REFERENCES", "baseline.pod_template.spec", [], [], "Template has no ConfigMap/Secret references.", "PASS"))
    for identity in sorted(refs):
        label = "/".join(identity)
        if saved is None or current is None or identity not in saved or identity not in current:
            output.append(_result("BASELINE_REFERENCE_EVIDENCE_MISSING", "baseline.reference_versions", label,
                                  "saved and current UID/resourceVersion", "Referenced configuration lacks trusted version evidence.", "UNKNOWN"))
        else:
            output.append(_result("BASELINE_REFERENCE_CHANGED", "live_state.reference_versions", {"reference": label, "uid": current[identity][0], "resource_version": current[identity][1]},
                                  {"uid": saved[identity][0], "resource_version": saved[identity][1]}, "Referenced configuration must match the version verified with the baseline.",
                                  "PASS" if saved[identity] == current[identity] else "FAIL"))
    return output


def referenced_objects(template, namespace):
    """Enumerate metadata-only identities for a read-only Kubernetes adapter.

    Return ``(identities, findings)``. The caller must preserve unsupported or
    malformed findings and must not interpret an empty list as verification.
    ``assess`` independently enumerates references when it validates the result.
    """
    try:
        mapping(template, "baseline.pod_template")
        string(namespace, "namespace")
        spec = mapping(template.get("spec"), "baseline.pod_template.spec")
        identities, findings = _references(spec, _containers(spec), namespace)
    except GuardInputError as exc:
        return [], [_invalid("baseline.pod_template", str(exc))]
    return [{"kind": kind, "namespace": ns, "name": name} for kind, ns, name in sorted(identities)], findings


def _registry(image):
    string(image, "image")
    if "://" in image or any(character.isspace() for character in image) or image.startswith("/"):
        raise GuardInputError("image: malformed image reference")
    first, separator, remainder = image.partition("/")
    host = first if separator and ("." in first or ":" in first or first == "localhost") else "docker.io"
    if not _REGISTRY.fullmatch(host) or (separator and not remainder):
        raise GuardInputError("image: malformed registry host")
    return host


def _security(spec, containers, live_state, rules):
    output = []

    def check(code, path, actual, expected, okay, message):
        output.append(_result(code, path, actual, expected, message, "PASS" if okay else "FAIL"))

    def flag(obj, key, path, default=False):
        value = obj.get(key, default)
        if type(value) is not bool:
            raise GuardInputError(f"{path}.{key}: expected a boolean")
        return value

    if rules.get("deny_host_access", False):
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            value = flag(spec, key, "baseline.pod_template.spec")
            check("SECURITY_HOST_ACCESS", f"baseline.pod_template.spec.{key}", value, False, not value, "Rollback must not share host namespaces.")
        volumes = spec.get("volumes", [])
        if not isinstance(volumes, list):
            raise GuardInputError("baseline.pod_template.spec.volumes: expected a list")
        for index, volume in enumerate(volumes):
            mapping(volume, f"baseline.pod_template.spec.volumes[{index}]")
            if "hostPath" in volume:
                check("SECURITY_HOST_ACCESS", f"baseline.pod_template.spec.volumes[{index}].hostPath", "present", "absent", False, "Rollback must not mount host filesystem paths.")
        for container, path in containers:
            ports = container.get("ports", [])
            if not isinstance(ports, list):
                raise GuardInputError(f"{path}.ports: expected a list")
            for index, port in enumerate(ports):
                mapping(port, f"{path}.ports[{index}]")
                host_port = port.get("hostPort", 0)
                if type(host_port) is not int:
                    raise GuardInputError(f"{path}.ports[{index}].hostPort: expected an integer")
                check("SECURITY_HOST_ACCESS", f"{path}.ports[{index}].hostPort", host_port, 0, host_port == 0, "Rollback must not bind a host port.")
    if rules.get("deny_privileged", False):
        pod_context = mapping(spec.get("securityContext", {}), "baseline.pod_template.spec.securityContext")
        for context, path in [(pod_context, "baseline.pod_template.spec.securityContext")] + [
            (mapping(container.get("securityContext", {}), f"{path}.securityContext"), f"{path}.securityContext") for container, path in containers
        ]:
            if path != "baseline.pod_template.spec.securityContext":
                privileged = flag(context, "privileged", path)
                escalation = flag(context, "allowPrivilegeEscalation", path, True)
                check("SECURITY_PRIVILEGED", f"{path}.privileged", privileged, False, not privileged, "Privileged containers are forbidden.")
                check("SECURITY_PRIVILEGE_ESCALATION", f"{path}.allowPrivilegeEscalation", escalation, False, not escalation, "Explicitly disable privilege escalation.")
                capabilities = mapping(context.get("capabilities", {}), f"{path}.capabilities")
                added = capabilities.get("add", [])
                if not isinstance(added, list) or any(not isinstance(item, str) for item in added):
                    raise GuardInputError(f"{path}.capabilities.add: expected a string list")
                check("SECURITY_CAPABILITIES", f"{path}.capabilities.add", added, [], not added, "Additional Linux capabilities are forbidden.")
                if "procMount" in context:
                    check("SECURITY_PROC_MOUNT", f"{path}.procMount", context["procMount"] if isinstance(context["procMount"], str) else "malformed", "Default", context["procMount"] == "Default", "Unmasked process mounts are forbidden.")
            if "runAsUser" in context:
                user = context["runAsUser"]
                if type(user) is not int or user < 0:
                    raise GuardInputError(f"{path}.runAsUser: expected a nonnegative integer")
                check("SECURITY_ROOT_USER", f"{path}.runAsUser", user, "nonzero UID", user != 0, "Rollback must not explicitly select the root user.")
            if "runAsNonRoot" in context:
                non_root = flag(context, "runAsNonRoot", path)
                check("SECURITY_ROOT_USER", f"{path}.runAsNonRoot", non_root, True, non_root, "Rollback must not explicitly allow a root user.")
            if "seccompProfile" in context:
                seccomp = mapping(context["seccompProfile"], f"{path}.seccompProfile")
                profile = seccomp.get("type")
                check("SECURITY_SECCOMP", f"{path}.seccompProfile.type", profile if isinstance(profile, str) else "malformed", ["RuntimeDefault", "Localhost"], profile in ("RuntimeDefault", "Localhost"), "An unconfined seccomp profile is forbidden.")
            if "windowsOptions" in context:
                windows = mapping(context["windowsOptions"], f"{path}.windowsOptions")
                host_process = flag(windows, "hostProcess", f"{path}.windowsOptions")
                check("SECURITY_PRIVILEGED", f"{path}.windowsOptions.hostProcess", host_process, False, not host_process, "Windows host-process containers are forbidden.")
    if "allowed_image_registries" in rules:
        for container, path in containers:
            registry = _registry(container.get("image"))
            check("SECURITY_IMAGE_REGISTRY", f"{path}.image", registry, rules["allowed_image_registries"], registry in rules["allowed_image_registries"], "Image registry must match an exact configured host.")
    account = spec.get("serviceAccountName", "default")
    if "allowed_service_accounts" in rules or rules.get("forbid_service_account_change", False):
        string(account, "baseline.pod_template.spec.serviceAccountName")
        if "allowed_service_accounts" in rules:
            check("SECURITY_SERVICE_ACCOUNT", "baseline.pod_template.spec.serviceAccountName", account, rules["allowed_service_accounts"], account in rules["allowed_service_accounts"], "Rollback service account must be allowlisted.")
        if rules.get("forbid_service_account_change", False):
            try:
                deployment = mapping(live_state.get("deployment"), "live_state.deployment")
                live_spec = mapping(mapping(mapping(deployment.get("spec"), "live_state.deployment.spec").get("template"), "live_state.deployment.spec.template").get("spec"), "live_state.deployment.spec.template.spec")
                current_account = live_spec.get("serviceAccountName", "default")
                string(current_account, "live_state.deployment.spec.template.spec.serviceAccountName")
            except GuardInputError:
                output.append(_result("SECURITY_SERVICE_ACCOUNT_UNKNOWN", "live_state.deployment.spec.template.spec.serviceAccountName", "unavailable", account, "Current service account cannot be verified.", "UNKNOWN"))
            else:
                check("SECURITY_SERVICE_ACCOUNT_CHANGE", "baseline.pod_template.spec.serviceAccountName", account, current_account, account == current_account, "Rollback must preserve the current service account.")
    return output


def _template_images(containers, path):
    """{container name: image} for one Pod template; GuardInputError on a duplicate name or a missing image."""
    images = {}
    for container, _ in containers:
        name, image = container.get("name"), container.get("image")
        if name in images or not isinstance(image, str):
            raise GuardInputError(f"{path}: container names must be unique and each needs an image")
        images[name] = image
    return images


def _current_images(live_state):
    """The current template's images, or None when the live template cannot be read."""
    try:
        deployment = mapping(live_state.get("deployment"), "live_state.deployment")
        spec = mapping(mapping(mapping(deployment.get("spec"), "live_state.deployment.spec").get("template"),
                               "live_state.deployment.spec.template").get("spec"), "live_state.deployment.spec.template.spec")
        return _template_images(_containers(spec), "live_state.deployment.spec.template.spec")
    except GuardInputError:
        return None


def _image_gate(containers, live_state, rules, checked_at):
    """The P5.02 CVE gate over every image the action would (re)create Pods from.

    Scans come only from the trusted reader as live_state.image_scans, so they
    are part of inputs_sha256 and a changed scan voids an earlier ALLOW. The
    candidate is the baseline template (a RESTART writes the same template plus
    a token). Absent scans are UNKNOWN (ESCALATE), never a pass.
    """
    paths = {container["name"]: path + ".image" for container, path in containers}
    try:
        candidate = _template_images(containers, "baseline.pod_template.spec")
    except GuardInputError as exc:
        return [_invalid("baseline.pod_template.spec", str(exc))]
    if "image_scans" not in live_state:
        return [_result("IMAGE_SCANS_MISSING", "live_state.image_scans", None, "trusted scan summaries for every image",
                        "No trusted image scan evidence was supplied; the CVE gate cannot pass.",
                        "FAIL" if rules.get("unknown_result") == "REJECT" else "UNKNOWN")]
    try:
        results = image_security.gate(live_state["image_scans"], candidate, _current_images(live_state), rules, checked_at)
    except ValueError as exc:
        return [_invalid("live_state.image_scans", str(exc))]
    return [_result(item["code"], paths.get(item["container"], "live_state.image_scans.exceptions"),
                    {"image": item["image"], "ids": item["ids"], "id_count": item["id_count"]},
                    {"mode": rules["mode"], "block_severities": rules["block_severities"]},
                    item["message"], item["status"]) for item in results]


def assess(proposal, live_state, baseline, policy, checked_at):
    """Return detailed PASS/FAIL/UNKNOWN/SKIP findings without performing I/O."""
    # A RESTART writes the baseline template plus a token, so new Pods are
    # created from it: the same health, security and reference rules apply.
    if proposal.get("action") not in ("ROLLBACK", "RESTART"):
        return [_result("ROLLBACK_ELIGIBILITY", "proposal.action", proposal.get("action"), "ROLLBACK or RESTART", "Template eligibility checks do not apply.", "SKIP")]
    baseline_rules = policy.get("baseline_constraints")
    security_rules = policy.get("security_constraints")
    if baseline_rules is None and security_rules is None:
        return [_result("ROLLBACK_ELIGIBILITY", "policy", "not configured", "optional constraints", "No additional rollback eligibility or security rules are configured.", "SKIP")]
    if baseline is None:
        return [_result("BASELINE_ELIGIBILITY_MISSING", "baseline", None, "trusted baseline", "Rollback eligibility cannot be established without a baseline.", "UNKNOWN")]
    try:
        mapping(baseline, "baseline")
        template = mapping(baseline.get("pod_template"), "baseline.pod_template")
        spec = mapping(template.get("spec"), "baseline.pod_template.spec")
        containers = _containers(spec)
        if "ephemeralContainers" in spec:
            raise GuardInputError("baseline.pod_template.spec.ephemeralContainers: not supported in Deployment rollback templates")
    except GuardInputError as exc:
        return [_invalid("baseline.pod_template", str(exc))]
    output = []
    if baseline_rules is not None:
        output.extend(_health(baseline, proposal, baseline_rules, checked_at))
        if baseline_rules.get("require_image_digests", False):
            for container, path in containers:
                image = container.get("image")
                okay = isinstance(image, str) and _DIGEST_IMAGE.fullmatch(image) is not None
                output.append(_result("BASELINE_IMAGE_DIGEST", f"{path}.image", image if isinstance(image, str) else "malformed", "repository@sha256:<64 lowercase hex characters>", "Rollback images must use immutable SHA-256 digests.", "PASS" if okay else "FAIL"))
        if baseline_rules.get("require_compatible_references", False):
            output.extend(_compatible_references(baseline, live_state, proposal, spec, containers))
    if security_rules is not None:
        try:
            output.extend(_security(spec, containers, live_state, security_rules))
        except GuardInputError as exc:
            output.append(_result("SECURITY_TEMPLATE_INVALID", "baseline.pod_template.spec", "malformed", "valid security-policy inputs", str(exc)))
        if "image_vulnerability_gate" in security_rules:
            output.extend(_image_gate(containers, live_state if isinstance(live_state, dict) else {},
                                      security_rules["image_vulnerability_gate"], checked_at))
    return output or [_result("ROLLBACK_ELIGIBILITY", "policy", "no enabled rules", "optional constraints", "No additional checks were enabled.", "SKIP")]
