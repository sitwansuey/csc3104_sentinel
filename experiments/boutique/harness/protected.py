"""Guarded recovery on the protected template (P5.05, E11; issues #69, #70, #10).

The security lab (scripts/security/boutique_security.py) deploys Online
Boutique with an mTLS gateway, an inspector and a TLS ingress sidecar beside
shippingservice. This module recovers that protected Deployment with the same
Guard, Executor and Verifier as the connected lab, with three changes:

- The Guard approves the protected template explicitly: the reviewed ingress
  sidecar only (services/guard/eligibility.py allowed_sidecars), its digest,
  its exact ConfigMap and Secret mounts, and the registries of Boutique and
  Envoy only.
- The identity that reads the referenced ConfigMap versions is a separate
  ServiceAccount, sentinel-guard-reader, with get on exactly those names
  (deploy/sentinel/guard-reader-rbac.yaml). The Executor keeps get and patch
  on its named Deployments only and never reads a Secret (#69). Since P5.06
  the reader reads no Secret either: the sidecar's identity Secret belongs to
  a cert-manager Certificate, and the reader gets that Certificate (Ready,
  notBefore, notAfter, revision, issuer and DNS names), which holds no key
  material. The Guard escalates an identity that is not Ready, expiring or
  not the reviewed one (certificate_identities). The token lasts one hour and
  the run deletes the reader's ServiceAccount, Role and RoleBinding at its end.
- The Verifier's health probe is authenticated: its Pod presents the
  sentinel-verifier-client certificate to the gateway, whose SAN allowlist
  admits it, and NetworkPolicy lets it reach the gateway's route ports only.

The live run is staged and documented in docs/security.md (Protected
recovery). Nothing here has been run against a cluster yet. The run holds the
heavy-lab lock (experiments/lab_lock.py), so it never runs alongside the
P4.01, P4.03, P4.05 or P5.07 connected-cluster runs.
"""

from copy import deepcopy
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import subprocess
import time

from experiments import lab_lock
from experiments.boutique.harness import targets
from experiments.boutique.harness.connected import APPLICATIONS, ConnectedBoutiqueExperiment, command
from experiments.boutique.harness.scenarios import CatalogError, load
from experiments.recovery.live import require, save, utc
from scripts.security import cert_lifecycle
from scripts.security.boutique_security_manifest import (ENVOY_RESOURCES, GATEWAY, NAMESPACE, VERIFIER_IDENTITY,
                                                         reviewed_sidecars, verifier_probe_pod)
from scripts.security.security_lab import CLIENT, ENVOY
from services.guard.adapters import read_certificate_status, read_reference_versions
from services.guard.common import quantity
from services.guard.eligibility import referenced_objects
from services.guard.preview import _private_file

ROOT = Path(__file__).resolve().parents[3]
PROTECTED_SCOPE = "protected"
CLUSTER = "sentinel-boutique-security"
CONTEXT = "kind-" + CLUSTER
GUARD_READER = "sentinel-guard-reader"
GUARD_READER_RBAC = ROOT / "deploy/sentinel/guard-reader-rbac.yaml"
READER_CONTEXT = "sentinel-guard-reader-boutique-security"
# Boutique's own images and the digest-pinned Envoy sidecar; the inspector
# (sentinel.local) runs only in the gateway Pod, never in a recovery target.
PROTECTED_REGISTRIES = ("us-central1-docker.pkg.dev", "docker.io")
STAGES = ("baseline", "full")
# An RBAC matrix row asked with kubectl auth can-i --all-namespaces (P5.06).
ALL_NAMESPACES = "*"
# The proto files the security lab publishes for its probes (boutique_security.prepare).
PROBE_SCHEMAS = {"hipstershop.ShippingService/GetQuote": "shipping.proto"}
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_DIGEST = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}")


def guard_reader_rbac(namespace, references, certificates=()):
    """The Guard-reader ServiceAccount, Role and RoleBinding: get on exactly these objects.

    references are the ConfigMap and Secret identities the reader must read
    itself (reader_scope() leaves out every Secret that a reviewed Certificate
    owns) and certificates the names of those cert-manager Certificates
    (P5.06). No list or watch, no other resource and no other name: kubectl
    auth can-i get secrets (without a name) is no.
    """
    if not isinstance(namespace, str) or not _NAME.fullmatch(namespace):
        raise ValueError("Invalid Guard-reader namespace")
    names = {"ConfigMap": set(), "Secret": set()}
    for reference in references:
        if (set(reference) != {"kind", "namespace", "name"} or reference["kind"] not in names
                or reference["namespace"] != namespace or not _NAME.fullmatch(reference["name"])):
            raise ValueError("The Guard reader reads only named ConfigMaps and Secrets in its namespace")
        names[reference["kind"]].add(reference["name"])
    certificates = sorted(set(certificates))
    if any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in certificates):
        raise ValueError("The Guard reader reads only named Certificates in its namespace")
    if not any(names.values()) and not certificates:
        raise ValueError("A template without references needs no Guard reader")
    rules = "".join(
        f"  - apiGroups: [\"{group}\"]\n    resources: [\"{resource}\"]\n    resourceNames: {json.dumps(sorted(values))}\n"
        "    verbs: [\"get\"]\n"
        for group, resource, values in (("", "configmaps", names["ConfigMap"]), ("", "secrets", names["Secret"]),
                                        ("cert-manager.io", "certificates", certificates)) if values)
    return (
        "# Generated by experiments/boutique/harness/protected.py guard_reader_rbac (P5.05, P5.06).\n"
        "# The Guard's reference reader: get on exactly the ConfigMaps that the protected\n"
        "# template references and the cert-manager Certificates that own its Secrets, so\n"
        "# neither the Executor (#69) nor this reader reads a Secret or its key; a\n"
        "# Certificate holds no key material. The token lasts one hour; the run deletes\n"
        "# these objects at its end, which revokes it (protected.py delete_reader_rbac).\n"
        "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
        f"  name: {GUARD_READER}\n  namespace: {namespace}\nautomountServiceAccountToken: false\n---\n"
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata:\n"
        f"  name: {GUARD_READER}\n  namespace: {namespace}\nrules:\n{rules}---\n"
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\nmetadata:\n"
        f"  name: {GUARD_READER}\n  namespace: {namespace}\nsubjects:\n  - kind: ServiceAccount\n"
        f"    name: {GUARD_READER}\n    namespace: {namespace}\nroleRef:\n"
        f"  apiGroup: rbac.authorization.k8s.io\n  kind: Role\n  name: {GUARD_READER}\n")


def template_references(template, namespace):
    """referenced_objects() of a template; ValueError when any reference form cannot be verified."""
    references, findings = referenced_objects(template, namespace)
    if findings:
        raise ValueError("The protected template has a reference the Guard cannot verify")
    return references


def rbac_matrix(namespace, executor_names, references, *, identities=(), other_secret="security-gateway"):
    """The expected kubectl auth can-i answers for the Executor and the Guard reader.

    Rows are (identity, verb, resource, expected). The Executor gets and
    patches its Deployments and reads nothing else, Secrets least of all; the
    reader gets exactly its named ConfigMaps and Secrets and nothing else.
    P5.06: identities are the Guard's certificate_identities. A Secret that one
    of them owns is read by neither identity; the reader gets that
    Certificate instead, and nothing else of cert-manager's. With them, the
    lab CA's key is a Secret in the cert-manager namespace behind a
    cluster-wide ClusterIssuer, so neither identity may mint one: no create
    of Certificates or CertificateRequests in the lab namespace or in any
    namespace, no get of either CA Secret and no patch of the issuer. A row
    outside the lab namespace carries "namespace" (ALL_NAMESPACES for
    --all-namespaces).
    """
    owned = {entry["secret"]: entry["certificate"] for entry in identities}
    secrets = sorted(item["name"] for item in references if item["kind"] == "Secret")
    readable = [name for name in secrets if name not in owned]
    certificates = sorted(owned[name] for name in secrets if name in owned)
    configs = sorted(item["name"] for item in references if item["kind"] == "ConfigMap")
    if other_secret in secrets:
        raise ValueError("The out-of-scope Secret must be one the reader may not read")
    certificate = "certificates.cert-manager.io/"
    rows = [("executor", verb, "deployment/" + name, True) for name in sorted(executor_names) for verb in ("get", "patch")]
    rows += [("executor", "get", "secrets", False), ("executor", "list", "secrets", False),
             *(("executor", "get", "secret/" + name, False) for name in secrets),
             *(("executor", "get", "configmap/" + name, False) for name in configs),
             *(("executor", "get", certificate + name, False) for name in certificates),
             ("executor", "delete", "deployment/" + sorted(executor_names)[0], False),
             ("executor", "patch", "deployment/paymentservice", False), ("executor", "create", "pods/exec", False)]
    patched = (("secret/" + readable[0]) if readable else ("configmap/" + configs[0]) if configs
               else certificate + certificates[0])
    rows += [*(("reader", "get", "secret/" + name, name not in owned) for name in secrets),
             *(("reader", "get", "configmap/" + name, True) for name in configs),
             *(("reader", "get", certificate + name, True) for name in certificates),
             ("reader", "get", "secret/" + other_secret, False), ("reader", "get", "secrets", False),
             ("reader", "list", "secrets", False), ("reader", "watch", "secrets", False),
             ("reader", "list", "configmaps", False), ("reader", "patch", patched, False),
             *((("reader", "list", "certificates.cert-manager.io", False),
                ("reader", "patch", certificate + certificates[0], False),
                ("reader", "get", "certificaterequests.cert-manager.io", False)) if certificates else ()),
             *(("reader", verb, "deployment/" + name, False) for name in sorted(executor_names) for verb in ("get", "patch")),
             ("reader", "create", "pods", False)]
    rows = [(*row, None) for row in rows]
    if certificates:
        manager = cert_lifecycle.CERT_MANAGER_NAMESPACE
        rows += [row for identity in ("executor", "reader") for row in (
            (identity, "create", "certificates.cert-manager.io", False, None),
            (identity, "create", "certificaterequests.cert-manager.io", False, None),
            (identity, "create", "certificates.cert-manager.io", False, ALL_NAMESPACES),
            (identity, "create", "certificaterequests.cert-manager.io", False, ALL_NAMESPACES),
            *((identity, "get", "secret/" + name, False, manager) for name in cert_lifecycle.CA_SECRETS),
            (identity, "patch", "clusterissuers.cert-manager.io/" + cert_lifecycle.ISSUER, False, None))]
    return [{"identity": identity, "verb": verb, "resource": resource, "allowed": allowed,
             **({"namespace": where} if where else {})}
            for identity, verb, resource, allowed, where in rows]


def _larger(first, second, resource):
    return first if quantity(first, resource, "ceiling") >= quantity(second, resource, "ceiling") else second


def protected_ceilings(ceilings):
    """The target's catalog ceilings, raised only as far as the reviewed Envoy sidecar needs.

    The Guard's resource ceilings apply to every container alike, so a
    container ceiling becomes the larger of the catalog's and the sidecar's own
    request or limit (for shipping: a 192Mi memory limit instead of 128Mi), and
    the Pod request ceiling covers the catalog's container request ceiling plus
    the sidecar's request. Nothing is lowered and nothing else changes.
    """
    result = deepcopy(ceilings)
    for key, side in (("container_requests", "requests"), ("container_limits", "limits")):
        for resource in ("cpu", "memory"):
            result[key][resource] = _larger(ceilings[key][resource], ENVOY_RESOURCES[side][resource], resource)
    for resource, unit, per_unit in (("cpu", "m", Fraction(1, 1000)), ("memory", "Mi", 1024 * 1024)):
        total = (quantity(ceilings["container_requests"][resource], resource, "ceiling")
                 + quantity(ENVOY_RESOURCES["requests"][resource], resource, "ceiling"))
        result["pod_requests"][resource] = _larger(ceilings["pod_requests"][resource],
                                                   f"{math.ceil(total / per_unit)}{unit}", resource)
    return result


def protected_policy_constraints(executor_names):
    """The Guard's security_constraints additions for the protected template.

    allowed_sidecars and registries (P5.05), and certificate_identities
    (P5.06): the cert-manager identity behind every Secret a reviewed sidecar
    mounts, so the Guard escalates when it is not Ready, expiring or not the
    reviewed identity.
    """
    sidecars = [entry for entry in reviewed_sidecars(ENVOY) if entry["target"] in executor_names]
    return {"allowed_image_registries": list(PROTECTED_REGISTRIES), "allowed_sidecars": sidecars,
            "certificate_identities": reviewed_identities(sidecars)}


def reviewed_identities(sidecars=None):
    """The Guard's certificate_identities for the Secrets that the reviewed sidecars mount (P5.06)."""
    sidecars = reviewed_sidecars(ENVOY) if sidecars is None else sidecars
    names = sorted({mount["source"]["name"][len("security-"):] for entry in sidecars for mount in entry["mounts"]
                    if mount["source"]["kind"] == "Secret" and mount["source"]["name"].startswith("security-")})
    return cert_lifecycle.guard_identities(names)


def reader_scope(references, identities):
    """(references the reader reads itself, Certificates it reads instead of their Secrets) for one template."""
    owned = {entry["secret"]: entry["certificate"] for entry in identities}
    own = [item for item in references if not (item["kind"] == "Secret" and item["name"] in owned)]
    return own, sorted({owned[item["name"]] for item in references
                        if item["kind"] == "Secret" and item["name"] in owned})


def preflight(security_run, *, stage, catalog=None, lock=None):
    """Problems that refuse a protected run before any cluster call; an empty list means go.

    Offline only: it reads the security lab's run folder, the committed
    reviewed files and the heavy-lab lock, and runs no kubectl, kind or docker.
    """
    problems = []
    if stage not in STAGES:
        problems.append("stage must be one of " + ", ".join(STAGES))
    if not _DIGEST.fullmatch(ENVOY) or not _DIGEST.fullmatch(CLIENT):
        problems.append("the Envoy and grpcurl images must be pinned by digest")
    for path in (ROOT / "scripts/security/security_lab.py", ROOT / "scripts/security/boutique_security_manifest.py",
                 GUARD_READER_RBAC):
        if "TODO-PIN" in path.read_text(encoding="utf-8"):
            problems.append(path.name + " still has an unresolved TODO-PIN")
    try:
        if cert_lifecycle.unresolved_pins(cert_lifecycle.load_pins(cert_lifecycle.PINS)):
            problems.append("deploy/cert-manager/pins.json still has an unresolved TODO-PIN (cert_lifecycle.py pins)")
    except (OSError, ValueError, AttributeError, TypeError):
        problems.append("deploy/cert-manager/pins.json cannot be read")
    try:
        scope = (catalog or load())["scopes"][PROTECTED_SCOPE]
        if (scope["cluster_id"], scope["context"], scope["namespace"]) != (CLUSTER, CONTEXT, NAMESPACE):
            problems.append("the catalog's protected scope is not the security lab")
        if targets.enabled_targets(PROTECTED_SCOPE, catalog) != ("shippingservice",):
            problems.append("the protected scope must offer shippingservice only")
    except (KeyError, CatalogError):
        problems.append("the catalog has no protected scope")
    try:
        expected = guard_reader_rbac(NAMESPACE, *reader_scope(reviewed_references(), reviewed_identities()))
        if GUARD_READER_RBAC.read_text(encoding="utf-8").replace("\r\n", "\n") != expected:
            problems.append("deploy/sentinel/guard-reader-rbac.yaml differs from the reviewed protected template")
    except (OSError, ValueError) as error:
        problems.append("the Guard-reader RBAC cannot be checked: " + type(error).__name__)
    folder = Path(security_run) if security_run is not None else None
    if folder is None or not folder.is_dir():
        problems.append("--security-run must name the security lab's run folder (boutique_security.py start)")
    else:
        try:
            session = json.loads((folder / "session.json").read_text(encoding="utf-8"))
            validation = json.loads((folder / "validation.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append("the security run has no readable session.json and validation.json")
        else:
            if (session.get("cluster"), session.get("context"), session.get("namespace")) != (CLUSTER, CONTEXT, NAMESPACE):
                problems.append("the security run is not the sentinel-boutique-security lab")
            if validation.get("passed") is not True:
                problems.append("the security lab's own validation has not passed")
            if not (validation.get("verifier_identity_quote") or {}).get("succeeded"):
                problems.append("the security lab predates the Verifier identity; restart it with this revision")
            if (session.get("certificates") or {}).get("issuer") != "cert-manager":
                problems.append("the security lab predates cert-manager identities (P5.06); restart it with this revision")
    try:
        (lock or lab_lock.HeavyLabLock(holder="P5.05 protected recovery")).check()
    except lab_lock.LabLockError as error:
        problems.append("heavy-lab lock: " + str(error))
    return problems


def reviewed_references():
    """The ConfigMaps and Secrets that the reviewed protected shippingservice template references."""
    entry = next(item for item in reviewed_sidecars(ENVOY) if item["target"] == "shippingservice")
    return sorted(({"kind": mount["source"]["kind"], "namespace": NAMESPACE, "name": mount["source"]["name"]}
                   for mount in entry["mounts"]), key=lambda item: (item["kind"], item["name"]))


def staged_commands(security_run, run_dir):
    """The documented live recipe, stage by stage (docs/security.md#protected-recovery); nothing runs here."""
    python = "python"
    harness = [python, "scripts/test_boutique_live.py", "--protected", "--security-run", str(security_run)]
    return {
        "offline": [[python, "-m", "unittest", "tests.test_guard_eligibility", "tests.test_live_executor",
                     "tests.test_security_connected", "tests.test_protected_recovery"],
                    [python, "scripts/security/protected_recovery.py", "rbac", "--check"]],
        "lab": [[python, "scripts/security/boutique_security.py", "start", "--directory", str(security_run),
                 "--exit-after-test"],
                [python, "scripts/security/protected_recovery.py", "preflight", "--security-run", str(security_run),
                 "--stage", "baseline"]],
        "baseline": [harness + ["--protected-stage", "baseline", "--approval", "interactive", "--run-dir",
                                str(run_dir) + "-baseline"]],
        "full": [harness + ["--protected-stage", "full", "--approval", "interactive", "--run-dir", str(run_dir)]],
        "standalone": [[python, "scripts/security/boutique_security.py", "test", "--directory", str(security_run),
                        "--exit-after-test"]],
    }


class ProtectedBoutiqueExperiment(ConnectedBoutiqueExperiment):
    """The connected harness on the security lab's protected shippingservice (P5.05).

    The lab must already run (boutique_security.py start). stage "baseline"
    stops after the protected baseline, the RBAC matrix and the before-repair
    bypass probes; "full" injects R1 and recovers it with one approved write.
    """

    cluster_id = CLUSTER
    context = CONTEXT
    namespace = NAMESPACE
    restricted_context = "sentinel-executor-boutique-security"
    reader_context = READER_CONTEXT
    health_source = "sentinel-boutique-security-authenticated"
    health_description = "Authenticated gRPC through the mTLS gateway, readiness and storefront"
    health_outcome_description = (" after executor rollback of the protected template; the authenticated Verifier "
                                  "published RECOVERED")
    stage = "full"
    # The heavy-lab lock this run holds (set by the live entry point); preflight accepts our own lock only.
    held_lock = None

    def __init__(self, directory, *, security_run, storefront_port=8080, stage="full"):
        super().__init__(directory, storefront_port=storefront_port)
        if stage not in STAGES:
            raise ValueError("Unknown protected stage")
        self.stage = stage
        self.security_run = Path(security_run).resolve()
        self.reader_credentials = self.directory / ".guard-reader-kubeconfig.json"
        self.reader_rbac_applied = False
        self.summary.update(scope="Protected security lab: mTLS gateway, inspector and TLS ingress sidecar; "
                                  "shipping-only recovery with a split Guard-reader identity",
                            protected_stage=stage)

    @property
    def allowed_registries(self):
        return list(PROTECTED_REGISTRIES)

    @property
    def ceilings(self):
        return protected_ceilings(self.profile.ceilings)

    # ----- preparation -----

    def prepare_image(self):
        """Use the running security lab; deploy nothing. Issue the Guard reader before the baseline needs it."""
        problems = preflight(self.security_run, stage=self.stage, lock=self.held_lock)
        require(not problems, "Protected preflight refused: " + "; ".join(problems))
        nodes = command(["kind", "get", "nodes", "--name", self.cluster_id]).split()
        require(nodes == [self.cluster_id + "-control-plane"], "The protected run requires the security lab's own node")
        reviewed = json.loads((ROOT / "deploy/boutique/connected-graph.json").read_text(encoding="utf-8"))
        self.graph = {"edge_direction": "caller_to_callee", "nodes": sorted(APPLICATIONS), "known_health": {},
                      "edges": reviewed["edges"]}
        save(self.directory / "dependency-graph.json", self.graph)
        self.client_image = CLIENT
        from services.observer.kubernetes import KubernetesEvidenceCollector, KubectlReader
        self.observer_collector = KubernetesEvidenceCollector(KubectlReader(self.context), namespace=self.namespace,
            name=self.name, cluster_id=self.cluster_id, collect_logs=True)
        self.write_executor_rbac()
        template = self.deployment()["spec"]["template"]
        require(template_references(template, self.namespace) == reviewed_references(),
                "The live protected template's references differ from the reviewed Guard-reader scope")
        self.issue_reader_credentials()
        self._start_storefront()
        self.summary.update(workload="Online Boutique v0.10.7 in the protected security lab",
            verification="Authenticated shipping gRPC through the gateway, readiness and storefront checkout")
        return next(container["image"] for container in template["spec"]["containers"]
                    if container["name"] == self.profile.container)

    def issue_reader_credentials(self):
        """Apply the reviewed Guard-reader RBAC and write its private one-hour kubeconfig (never logged)."""
        self.reader_rbac_applied = True
        self.kubectl("apply", "-f", str(GUARD_READER_RBAC))
        self._write_private_kubeconfig(self.reader_credentials, GUARD_READER, self.reader_context, "guard-reader")

    def delete_reader_rbac(self):
        """Delete the Guard reader's ServiceAccount, Role and RoleBinding; True when kubectl confirmed it.

        Deleting the ServiceAccount revokes its bound one-hour token at once,
        so the reader's residual Secret read ends with the run rather than up
        to an hour later. A failed delete is recorded, not raised, so it never
        hides the run's own outcome; docs/security.md gives the manual command.
        """
        try:
            result = self.kubectl("delete", "-f", str(GUARD_READER_RBAC), "--ignore-not-found", "--wait=false",
                                  allow_failure=True)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _write_private_kubeconfig(self, path, account, context, user):
        """A one-hour token for account in a private kubeconfig, as the base harness writes the Executor's."""
        configuration = json.loads(self.kubectl("config", "view", "--minify", "--flatten", "--raw", "-o", "json").stdout)
        cluster = configuration["clusters"][0]["cluster"]
        require("certificate-authority-data" in cluster and cluster["server"].startswith("https://"),
                "Restricted credentials require the selected cluster's CA and HTTPS server")
        token = self.kubectl("create", "token", account, "--duration=1h").stdout.strip()
        private = {"apiVersion": "v1", "kind": "Config",
                   "clusters": [{"name": self.cluster_id, "cluster": {
                       "server": cluster["server"], "certificate-authority-data": cluster["certificate-authority-data"]}}],
                   "users": [{"name": user, "user": {"token": token}}],
                   "contexts": [{"name": context, "context": {"cluster": self.cluster_id, "user": user,
                                                              "namespace": self.namespace}}],
                   "current-context": context}
        with path.open("x", encoding="utf-8") as stream:
            _private_file(path)
            json.dump(private, stream)

    def reader_scope(self, template, current=None):
        """(ConfigMap and Secret references, Certificate names) the reader reads for one or two templates."""
        references = template_references(template, self.namespace)
        if current is not None:
            references += template_references(current, self.namespace)
        return reader_scope(references, reviewed_identities())

    def read_references(self, template, current=None):
        """Reference versions of one or two templates, read with the Guard reader only; never an owned Secret."""
        references, _ = self.reader_scope(template, current)
        return read_reference_versions(self.target, references, context=self.reader_context,
                                       cluster_id=self.cluster_id, kubeconfig=self.reader_credentials)

    def read_certificates(self, template, current=None):
        """The status of the Certificates behind those templates' identity Secrets, read with the Guard reader (P5.06)."""
        _, certificates = self.reader_scope(template, current)
        return read_certificate_status(self.target, certificates, context=self.reader_context,
                                       cluster_id=self.cluster_id, kubeconfig=self.reader_credentials)

    def measured_baseline(self, template, samples):
        baseline = super().measured_baseline(template, samples)
        baseline["reference_versions"] = self.read_references(template)
        baseline["certificates"] = self.read_certificates(template)
        return baseline

    def executor_policy(self):
        policy = super().executor_policy()
        policy["security_constraints"].update(protected_policy_constraints(self.executor_names()))
        return policy

    def guard_state(self, proposal):
        """The Coordinator Guard's state, with reference versions read by the Guard reader like the Executor's check."""
        state = super().guard_state(proposal)
        state["reference_versions"] = self.read_references(self.baseline["pod_template"],
                                                           state["deployment"]["spec"]["template"])
        state["certificates"] = self.read_certificates(self.baseline["pod_template"],
                                                       state["deployment"]["spec"]["template"])
        return state

    def reader_inputs(self):
        return {"reference_kubeconfig": self.reader_credentials, "reference_context": self.reader_context}

    def reader_arguments(self):
        return ["--reference-kubeconfig", str(self.reader_credentials), "--reference-context", self.reader_context]

    # ----- authenticated Verifier probe -----

    def probe_pod(self, name, method, request):
        """The Verifier's probe for this target: authenticated, through the gateway, admitted only there."""
        require(method in PROBE_SCHEMAS, "The protected probe supports the shipping quote only")
        return verifier_probe_pod(name, image=self.client_image, route="shipping", method=method, request=request,
                                  schema=PROBE_SCHEMAS[method])

    def install_probe_schema(self):
        """The security lab already publishes security-test-proto; nothing to install."""

    # ----- RBAC matrix and bypass probes -----

    def permissions(self):
        """The Executor's base can-i checks, then the full Executor and Guard-reader matrix."""
        super().permissions()
        references = template_references(self.baseline["pod_template"], self.namespace)
        rows = rbac_matrix(self.namespace, self.executor_names(), references, identities=reviewed_identities())
        results = []
        for row in rows:
            restricted = self.credentials if row["identity"] == "executor" else self.reader_credentials
            context = self.restricted_context if row["identity"] == "executor" else self.reader_context
            result = self.kubectl_as(restricted, context, "auth", "can-i", row["verb"], row["resource"],
                                     namespace=row.get("namespace"))
            answer = result.stdout.strip()
            where = " in " + row["namespace"] if row.get("namespace") else ""
            require(answer == ("yes" if row["allowed"] else "no"),
                    f"RBAC can-i for {row['identity']} {row['verb']} {row['resource']}{where} was {answer or 'unknown'}")
            results.append(row)
        save(self.directory / "protected-rbac-checks.json", results)
        self.check("Executor and Guard-reader RBAC can-i matrix: no Secret read by either; reader gets only its names",
                   rows=len(results))
        self.bypass_checks("before")

    def kubectl_as(self, kubeconfig, context, *arguments, namespace=None):
        """kubectl with a restricted kubeconfig and context; the answer is checked by the caller.

        namespace defaults to the lab's; ALL_NAMESPACES appends can-i's own
        --all-namespaces after the arguments, which overrides the namespace.
        """
        import subprocess
        everywhere = namespace == ALL_NAMESPACES
        scope = self.namespace if everywhere or not namespace else namespace
        return subprocess.run(["kubectl", "--context=" + context, "--namespace=" + scope,
                               "--request-timeout=10s", "--kubeconfig=" + str(kubeconfig), *arguments,
                               *(["--all-namespaces"] if everywhere else [])],
                              capture_output=True, text=True, timeout=30, check=False)

    def bypass_checks(self, stage):
        """Direct Service, Pod IP and gateway-outage bypasses refused, each beside a positive control.

        Uses the security lab's own probe Pods (boutique_security.probe), so
        the checks are the ones its validation runs, and like its validation
        each denial counts only next to an authenticated call through the
        gateway that succeeds with the same probe image, Secret mount and
        schema: a broken probe environment would otherwise look like every
        bypass refused. The control runs before the outage and again after the
        gateway is scaled back up and rolled out, so the run never continues
        with the gateway down. Before and after the repair the control is the
        Verifier's authenticated shipping quote; during the fault, which
        breaks shipping, it is the lab client's authenticated cart call
        (GetCart) through the gateway, which does not depend on shipping. A
        shipping Pod without an IP is recorded as not attempted and fails the
        check, so the Pod-IP bypass is never claimed without a probe.
        """
        from scripts.security import boutique_security as lab

        def control():
            if stage == "during":
                return lab.probe(self.directory, route="cart")
            return lab.probe(self.directory, identity=VERIFIER_IDENTITY)

        controls = {"before_outage": control()}
        results = {"direct_service_bypass": lab.probe(self.directory, target="shippingservice:50051", secure=False)}
        pods = json.loads(self.kubectl("get", "pods", "-l", lab.workload("shippingservice"), "-o", "json").stdout)["items"]
        address = next((pod.get("status", {}).get("podIP") for pod in pods if pod.get("status", {}).get("podIP")), None)
        results["direct_pod_ip_bypass"] = (lab.probe(self.directory, target=address + ":50051", secure=False)
                                           if address else None)
        self.kubectl("scale", "deployment/" + GATEWAY[0], "--replicas=0")
        try:
            deadline = time.monotonic() + 60
            while json.loads(self.kubectl("get", "pods", "-l", lab.workload(GATEWAY[0]), "-o", "json").stdout)["items"]:
                require(time.monotonic() < deadline, "The gateway did not stop for the outage check")
                time.sleep(.5)
            results["gateway_outage"] = lab.probe(self.directory, identity=VERIFIER_IDENTITY)
            results["outage_direct_bypass"] = lab.probe(self.directory, target="shippingservice:50051", secure=False)
        finally:
            self.kubectl("scale", "deployment/" + GATEWAY[0], "--replicas=1")
            self.kubectl("rollout", "status", "deployment/" + GATEWAY[0], "--timeout=120s", timeout=140)
        controls["after_restore"] = control()
        not_attempted = sorted(key for key, value in results.items() if value is None)
        blocked = {key: value["succeeded"] is not True for key, value in results.items() if value is not None}
        controlled = {key: value["succeeded"] is True for key, value in controls.items()}
        passed = all(blocked.values()) and all(controlled.values()) and not not_attempted
        self.summary.setdefault("bypass_checks", {})[stage] = passed
        save(self.directory / ("bypass-" + stage + ".json"), {
            "bypasses": {key: ({"attempted": False} if value is None else
                               {"attempted": True, "succeeded": value["succeeded"] is True})
                         for key, value in results.items()},
            "positive_control": {"call": ("lab client cart GetCart through the gateway" if stage == "during" else
                                          "Verifier shipping GetQuote through the gateway"),
                                 **{key: {"succeeded": value} for key, value in controlled.items()}},
            "passed": passed})
        require(all(blocked.values()), "A bypass of the protected route succeeded " + stage + " repair: "
                + ", ".join(key for key, value in blocked.items() if not value))
        require(all(controlled.values()), "The authenticated positive control failed " + stage + " repair ("
                + ", ".join(key for key, value in controlled.items() if not value)
                + "), so the refused bypasses prove nothing")
        require(not not_attempted, "A bypass probe was not attempted " + stage + " repair: " + ", ".join(not_attempted))
        self.check("bypass probes blocked " + stage + " repair: direct Service, Pod IP and gateway outage; "
                   "authenticated control call succeeded before and after the outage", stage=stage)

    def require_bypass_time_points(self, stages):
        """Before any PASS: the bypass checks ran at exactly these time points and every one passed."""
        recorded = self.summary.get("bypass_checks", {})
        require(set(recorded) == set(stages) and all(recorded[stage] is True for stage in stages),
                "Bypass probes did not pass at every time point: expected " + ", ".join(stages) + "; recorded "
                + (", ".join(f"{key}={value}" for key, value in sorted(recorded.items())) or "none"))

    # ----- the staged run -----

    def fault(self, label):
        incident = super().fault(label)
        if incident is not None:
            self.bypass_checks("during")
        return incident

    def recover_incident(self, incident):
        """Zero-write negative cases, then one approved write verified by the authenticated probe."""
        self.negative_cases(incident)
        self.positive(incident)
        self.bypass_checks("after")
        from scripts.security import boutique_security as lab
        lab.collect_inspections(self.directory)

    def after_recovery_scenarios(self):
        """No third proposal: the protected run's acceptance is one write (P5.05).

        The base run marks PASS only after this returns, so the bypass checks
        must have passed before, during and after the repair.
        """
        self.require_bypass_time_points(("before", "during", "after"))

    def run(self):
        self.summary["protected_stage"] = self.stage
        try:
            if self.stage == "baseline":
                self.run_baseline()
            else:
                super().run()
        finally:
            self.reader_credentials.unlink(missing_ok=True)
            self.summary["private_reader_credentials_removed"] = not self.reader_credentials.exists()
            if self.reader_rbac_applied:
                self.summary["guard_reader_rbac_deleted"] = self.delete_reader_rbac()
            save(self.directory / "summary.json", self.summary)

    def run_baseline(self):
        """Stage (b): the protected baseline, the RBAC matrix and the before-repair bypass probes; no fault."""
        try:
            self.prepare()
            self.permissions()
            self.require_bypass_time_points(("before",))
            self.summary["status"] = "PASS"
        except BaseException as exc:
            self.summary.update(status="FAILED", error_type=type(exc).__name__, error=str(exc)[:300])
            raise
        finally:
            self.credentials.unlink(missing_ok=True)
            self.close_approval_authority()
            self.store.close()
            self.summary.update(finished_at=utc(), private_executor_credentials_removed=not self.credentials.exists())
            save(self.directory / "summary.json", self.summary)

    def close(self):
        super().close()
        self.reader_credentials.unlink(missing_ok=True)
