"""Certificate lifecycle for the protected security lab: cert-manager and Envoy file-based SDS (P5.06, E12; #70).

One issuer for every lab identity: cert-manager, with a CA ClusterIssuer
seeded from a lab CA made locally at start. Each identity is a Certificate
(exact DNS SAN, two-hour lifetime, renewed an hour before expiry, a new key
on every renewal) whose Secret security-<identity> every Envoy mounts as a
whole directory, without subPath. Envoy reads the key pair and the trust
bundle through file-based SDS with watched_directory
(scripts/security/security_lab.py::tls_context), so a renewal is served
without restarting any container. The trust bundle is a public ConfigMap,
security-trust-bundle; during a CA rollover it holds the old and the new CA
until every certificate is reissued, then only the new one.

Subcommands, none of which runs a cluster command unless named live:

  pins       Print the unresolved cert-manager pins and the commands that
             resolve them; --check exits 1 while any TODO-PIN is left. The
             lab refuses to start until none is left.
  plan       Print the live recipe (docs/security.md, Certificate lifecycle)
             as JSON. Nothing runs.
  negatives  Live: make the refused test identities (untrusted CA, wrong SAN,
             a 10-minute certificate that has already expired) and apply
             them as Secrets for boutique_security.py probe --identity.
  rollover   Live: start, switch or finish a CA rollover (see plan).
  identity   Live, read-only: read every lab Certificate's status and print
             what the Guard's certificate_identities rule decides (exit 1
             unless every identity passes).

The lab CA key and every test key stay in the run folder's private pki
directory (owner-only on Windows, services/guard/preview._private_file),
never in the repository, a manifest, a log or a summary.
"""

import argparse
from datetime import UTC, datetime, timedelta
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.security.boutique_security_manifest import NAMESPACE, TRUST_BUNDLE, VERIFIER_IDENTITY
from services.guard.preview import _private_file

CLUSTER = "sentinel-boutique-security"
CONTEXT = "kind-" + CLUSTER
PINS = ROOT / "deploy/cert-manager/pins.json"
CERT_MANAGER_NAMESPACE = "cert-manager"
ISSUER = "sentinel-lab-ca"
UNTRUSTED_ISSUER = "sentinel-untrusted-ca"
ISSUER_REF = {"group": "cert-manager.io", "kind": "ClusterIssuer", "name": ISSUER}
# The Secrets in the cert-manager namespace that hold a lab CA key: the first
# CA and the one a rollover switches to. Neither the Executor nor the Guard
# reader may get them (experiments/boutique/harness/protected.py::rbac_matrix).
ROLLOVER_CA_SECRET = ISSUER + "-2"
CA_SECRETS = (ISSUER, ROLLOVER_CA_SECRET)
# Every lab identity and its TLS role; the Secret is security-<identity>, the SAN <identity>.internal.
IDENTITIES = (("gateway", "server"), ("upstream", "server"), ("cart-upstream", "server"),
              ("proxy-client", "client"), ("client", "client"), ("frontend-client", "client"),
              ("checkoutservice-client", "client"), (VERIFIER_IDENTITY, "client"))
# cert-manager refuses a duration under one hour and a renewBefore under five
# minutes, so the shortest profile renews every ten minutes (rotation); a
# certificate that is already expired is made locally instead (negatives).
PROFILES = {"lab": {"duration": "2h", "renewBefore": "1h"},
            "rotation": {"duration": "1h", "renewBefore": "50m"}}
# The Guard escalates a mounted identity with less validity left than this
# (services/guard/eligibility.py::_certificate_identities). A healthy lab
# certificate keeps at least 50 minutes (rotation) or an hour (lab).
GUARD_MIN_REMAINING_SECONDS = 1800
# The refused test identities: SAN, signing CA and validity window.
NEGATIVE_IDENTITIES = {
    "negative-untrusted-ca": {"san": "client.internal", "ca": "untrusted"},
    "negative-wrong-san": {"san": "wrong-client.internal", "ca": "lab"},
    "negative-expired": {"san": "client.internal", "ca": "lab", "expired_minutes": 10},
}
# Signed by the old CA at rollover start: accepted during the overlap, refused after it.
OLD_CA_IDENTITY = "negative-old-ca"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PEM = re.compile(r"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\r?\n?")


def run(args, *, timeout=300, required=True):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    if required and result.returncode:
        # These commands carry paths and names only, never key or certificate contents.
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-1500:]}")
    return result


def kubectl(command, *args, **kwargs):
    """kubectl on the security lab only, with no default namespace (each object names its own)."""
    return command(["kubectl", "--context=" + CONTEXT, *args], **kwargs)


# ----- pins -----

def load_pins(path=PINS):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def unresolved_pins(pins):
    """Every pin that is not a resolved value; an empty list means the lab may install cert-manager."""
    problems = []
    if not re.fullmatch(r"v\d+\.\d+\.\d+", str(pins.get("version", ""))):
        problems.append("version: expected vMAJOR.MINOR.PATCH")
    chart = pins.get("chart", {})
    if chart.get("url") != f"https://charts.jetstack.io/charts/cert-manager-{pins.get('version')}.tgz":
        problems.append("chart.url: expected the jetstack chart of the pinned version")
    if not _HEX64.fullmatch(str(chart.get("sha256", ""))):
        problems.append("chart.sha256: " + str(chart.get("sha256")))
    for name, image in sorted(pins.get("images", {}).items()):
        if not _DIGEST.fullmatch(str(image.get("digest", ""))):
            problems.append(f"images.{name}.digest: " + str(image.get("digest")))
    if set(pins.get("images", {})) != {"controller", "webhook", "cainjector", "startupapicheck", "acmesolver"}:
        problems.append("images: expected controller, webhook, cainjector, startupapicheck and acmesolver")
    return problems


def pin_commands(pins):
    """The commands that resolve each pin (run them by hand on a networked machine, then edit pins.json)."""
    version = pins["version"]
    commands = [f"curl -fsSLo cert-manager-{version}.tgz {pins['chart']['url']}",
                f"(Get-FileHash -Algorithm SHA256 cert-manager-{version}.tgz).Hash.ToLower()   # chart.sha256"]
    for name, image in sorted(pins["images"].items()):
        commands.append(f"docker buildx imagetools inspect {image['repository']}:{version} "
                        "--format \"{{.Manifest.Digest}}\"   # images." + name + ".digest")
    return commands


def image_refs(pins):
    return [image["repository"] + "@" + image["digest"] for _, image in sorted(pins["images"].items())]


def helm_values(pins):
    """The chart values: digest-pinned images only, CRDs installed by the chart, small requests, no metrics."""
    def image(name):
        return {"repository": pins["images"][name]["repository"], "digest": pins["images"][name]["digest"],
                "pullPolicy": "IfNotPresent"}
    small = {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}}
    return {"crds": {"enabled": True}, "replicaCount": 1, "image": image("controller"), "resources": small,
            "webhook": {"image": image("webhook"), "resources": small},
            "cainjector": {"image": image("cainjector"), "resources": small},
            "startupapicheck": {"image": image("startupapicheck")},
            "acmesolver": {"image": image("acmesolver")},
            "prometheus": {"enabled": False}}


def install(directory, node, command, *, pins=None, fetch=None):
    """Live: install the pinned cert-manager chart into the security lab; refuses unresolved pins first."""
    pins = pins or load_pins()
    problems = unresolved_pins(pins)
    if problems:
        raise ValueError("cert-manager pins are unresolved (scripts/security/cert_lifecycle.py pins): "
                         + "; ".join(problems))
    # charts.jetstack.io refuses urllib's default User-Agent with 403 (seen 14 Nov 2026); the SHA-256 pin, checked
    # below, is what makes the download trustworthy, not the client name.
    fetch = fetch or (lambda url: urlopen(Request(url, headers={"User-Agent": "sentinel-security-lab/1.0"}),
                                          timeout=60).read(10_000_000))
    chart = fetch(pins["chart"]["url"])
    if hashlib.sha256(chart).hexdigest() != pins["chart"]["sha256"]:
        raise ValueError("cert-manager chart checksum changed")
    path = directory / ("cert-manager-" + pins["version"] + ".tgz")
    path.write_bytes(chart)
    values = directory / "cert-manager-values.json"
    values.write_text(json.dumps(helm_values(pins), indent=2) + "\n", encoding="utf-8")
    for image in image_refs(pins):
        command(["docker", "pull", image], timeout=600)
        command(["docker", "exec", node, "crictl", "pull", image], timeout=600)
    command(["helm", "upgrade", "--install", "cert-manager", path, "--kube-context", CONTEXT,
             "--namespace", CERT_MANAGER_NAMESPACE, "--create-namespace", "--values", values,
             "--wait", "--timeout", "5m"], timeout=360)


# ----- resources (pure) -----

def certificates(profile="lab"):
    """One Certificate per lab identity: exact DNS SAN, the profile's lifetime and a new key on every renewal."""
    lifetime = PROFILES[profile]
    items = []
    for identity, role in IDENTITIES:
        host = identity + ".internal"
        items.append({"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
                      "metadata": {"name": "security-" + identity, "namespace": NAMESPACE},
                      "spec": {"secretName": "security-" + identity, "commonName": host, "dnsNames": [host],
                               "duration": lifetime["duration"], "renewBefore": lifetime["renewBefore"],
                               "usages": ["digital signature", "key encipherment",
                                          "server auth" if role == "server" else "client auth"],
                               "privateKey": {"algorithm": "RSA", "size": 2048, "rotationPolicy": "Always"},
                               "issuerRef": dict(ISSUER_REF)}})
    return items


def cluster_issuer(name, secret):
    return {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": name},
            "spec": {"ca": {"secretName": secret}}}


def trust_bundle(pems):
    """The public trust-bundle ConfigMap: the given CA certificates only, in order."""
    if not pems or any(not _PEM.fullmatch(pem) or "PRIVATE KEY" in pem for pem in pems):
        raise ValueError("The trust bundle holds CA certificates only")
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": TRUST_BUNDLE, "namespace": NAMESPACE},
            "data": {"ca.crt": "".join(pem if pem.endswith("\n") else pem + "\n" for pem in pems)}}


def guard_identities(identities):
    """The Guard's certificate_identities for these lab identities (services/guard/eligibility.py)."""
    return [{"secret": "security-" + identity, "certificate": "security-" + identity, "issuer": dict(ISSUER_REF),
             "dns_names": [identity + ".internal"], "min_remaining_seconds": GUARD_MIN_REMAINING_SECONDS}
            for identity in identities]


# ----- local key material (run folder only) -----

def write_private(path, text):
    """A new owner-only file; it never replaces an existing one."""
    with Path(path).open("x", encoding="utf-8") as stream:
        _private_file(path)
        stream.write(text)


def private_key_file(path):
    """An empty owner-only file for openssl to write a key into.

    It is restricted before any key is in it: openssl overwrites an existing
    file in place, which keeps its permissions, so the key is never readable
    under inherited permissions (on Windows mkdir's mode has no effect).
    """
    with Path(path).open("x", encoding="utf-8"):
        _private_file(path)
    return path


def make_ca(pki, name, *, command=run, days=30):
    """A lab CA in pki/<name>: ca.crt (public) and ca.key (owner-only)."""
    folder = Path(pki) / name
    folder.mkdir(mode=0o700)
    private_key_file(folder / "ca.key")
    command([shutil.which("openssl") or "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", str(days), "-subj", "/CN=Sentinel lab CA " + name,
             "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
             "-addext", "subjectKeyIdentifier=hash", "-keyout", folder / "ca.key", "-out", folder / "ca.crt"])
    _private_file(folder / "ca.key")
    return folder


def _secret(name, namespace, folder, keys, *, kind="Opaque"):
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": namespace}, "type": kind,
            "data": {key: base64.b64encode((folder / source).read_bytes()).decode() for key, source in keys.items()}}


def apply_private(command, folder, name, resource):
    """Write a resource holding key material to an owner-only file and apply it; nothing is logged."""
    path = folder / (name + ".json")
    write_private(path, json.dumps(resource))
    kubectl(command, "apply", "-f", path)


def _utc(value):
    return value.strftime("%y%m%d%H%M%SZ")


def sign(folder, ca, san, *, command=run, expired_minutes=None, now=None):
    """A client key pair in folder, signed by the CA in ca, for one DNS SAN.

    With expired_minutes the certificate is valid for exactly that many minutes
    ending one minute before now (openssl ca with explicit dates), so it is
    already expired; otherwise it is valid for one day.
    """
    openssl = shutil.which("openssl") or "openssl"
    folder.mkdir(mode=0o700)
    private_key_file(folder / "tls.key")
    command([openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=" + san,
             "-keyout", folder / "tls.key", "-out", folder / "tls.csr"])
    _private_file(folder / "tls.key")
    extension = folder / "tls.ext"
    extension.write_text("basicConstraints=critical,CA:FALSE\nsubjectAltName=DNS:" + san + "\n"
                         "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\n",
                         encoding="utf-8")
    if expired_minutes is None:
        command([openssl, "x509", "-req", "-in", folder / "tls.csr", "-CA", ca / "ca.crt", "-CAkey", ca / "ca.key",
                 "-CAcreateserial", "-days", "1", "-extfile", extension, "-out", folder / "tls.crt"])
    else:
        end = (now or datetime.now(UTC)) - timedelta(minutes=1)
        start = end - timedelta(minutes=expired_minutes)
        (folder / "index.txt").write_text("", encoding="utf-8")
        (folder / "serial").write_text("1000\n", encoding="utf-8")
        config = folder / "ca.cnf"
        config.write_text("[ca]\ndefault_ca = lab\n[lab]\n"
                          f"database = {(folder / 'index.txt').as_posix()}\nnew_certs_dir = {folder.as_posix()}\n"
                          f"serial = {(folder / 'serial').as_posix()}\ndefault_md = sha256\npolicy = anything\n"
                          "unique_subject = no\ncopy_extensions = none\n[anything]\ncommonName = supplied\n",
                          encoding="utf-8")
        command([openssl, "ca", "-batch", "-notext", "-config", config, "-cert", ca / "ca.crt",
                 "-keyfile", ca / "ca.key", "-in", folder / "tls.csr", "-out", folder / "tls.crt",
                 "-startdate", _utc(start), "-enddate", _utc(end), "-extfile", extension])
    return folder


# ----- live steps -----

def issue(directory, ca, command, *, profile="lab"):
    """Live: seed the CA issuer, publish the trust bundle and request every lab Certificate, then wait for Ready."""
    pki = directory / "pki"
    apply_private(command, pki, "ca-secret-" + ca.name,
                  _secret(ISSUER, CERT_MANAGER_NAMESPACE, ca, {"tls.crt": "ca.crt", "tls.key": "ca.key"},
                          kind="kubernetes.io/tls"))
    public = {"apiVersion": "v1", "kind": "List",
              "items": [cluster_issuer(ISSUER, ISSUER),
                        trust_bundle([(ca / "ca.crt").read_text(encoding="utf-8")]), *certificates(profile)]}
    path = directory / "certificates.json"
    path.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")
    kubectl(command, "apply", "-f", path)
    kubectl(command, "--namespace=" + NAMESPACE, "wait", "--for=condition=Ready", "certificates.cert-manager.io",
            "--all", "--timeout=300s", timeout=320)
    return public


def negatives(directory, command, *, now=None):
    """Live: the refused test identities, each as Secret security-<name> with the lab CA as ca.crt."""
    pki = directory / "pki"
    lab = pki / "ca-1"
    untrusted = make_ca(pki, "untrusted-ca", command=command)
    made = []
    for name, spec in NEGATIVE_IDENTITIES.items():
        folder = sign(pki / name, untrusted if spec["ca"] == "untrusted" else lab, spec["san"], command=command,
                      expired_minutes=spec.get("expired_minutes"), now=now)
        shutil.copyfile(lab / "ca.crt", folder / "ca.crt")
        apply_private(command, pki, "secret-" + name,
                      _secret("security-" + name, NAMESPACE, folder,
                              {"ca.crt": "ca.crt", "tls.crt": "tls.crt", "tls.key": "tls.key"}))
        made.append(name)
    return made


def rollover(directory, step, command):
    """Live CA rollover: start (bundle = old + new), switch (issue from the new CA), finish (bundle = new only)."""
    pki = directory / "pki"
    old, new = pki / "ca-1", pki / "ca-2"
    if step == "start":
        make_ca(pki, "ca-2", command=command)
        folder = sign(pki / OLD_CA_IDENTITY, old, "client.internal", command=command)
        # The probe trusts both CAs, so a refusal after the rollover is the gateway's, not the client's.
        (folder / "ca.crt").write_text((old / "ca.crt").read_text(encoding="utf-8")
                                       + (new / "ca.crt").read_text(encoding="utf-8"), encoding="utf-8")
        apply_private(command, pki, "secret-" + OLD_CA_IDENTITY,
                      _secret("security-" + OLD_CA_IDENTITY, NAMESPACE, folder,
                              {"ca.crt": "ca.crt", "tls.crt": "tls.crt", "tls.key": "tls.key"}))
        bundle = [old, new]
    elif step == "switch":
        apply_private(command, pki, "ca-secret-ca-2",
                      _secret(ROLLOVER_CA_SECRET, CERT_MANAGER_NAMESPACE, new, {"tls.crt": "ca.crt", "tls.key": "ca.key"},
                              kind="kubernetes.io/tls"))
        path = directory / "issuer-ca-2.json"
        path.write_text(json.dumps(cluster_issuer(ISSUER, ROLLOVER_CA_SECRET), indent=2) + "\n", encoding="utf-8")
        kubectl(command, "apply", "-f", path)
        # Deleting a Secret makes cert-manager reissue it from the issuer's new CA;
        # the Pods keep their mounted files until the new Secret arrives.
        for identity, _ in IDENTITIES:
            kubectl(command, "--namespace=" + NAMESPACE, "delete", "secret", "security-" + identity, "--wait=false")
        kubectl(command, "--namespace=" + NAMESPACE, "wait", "--for=condition=Ready", "certificates.cert-manager.io",
                "--all", "--timeout=300s", timeout=320)
        return None
    elif step == "finish":
        bundle = [new]
    else:
        raise ValueError("Unknown rollover step")
    path = directory / ("trust-bundle-" + step + ".json")
    path.write_text(json.dumps(trust_bundle([(ca / "ca.crt").read_text(encoding="utf-8") for ca in bundle]),
                               indent=2) + "\n", encoding="utf-8")
    kubectl(command, "apply", "-f", path)
    return [ca.name for ca in bundle]


def identity_report(records, now):
    """What the Guard's certificate_identities rule decides about each lab Certificate (read-only, P5.06).

    {"status": PASS or ESCALATE or INVALID, "findings": [...]} for status
    records from services/guard/adapters.py::read_certificate_status.
    """
    from services.guard.eligibility import certificate_findings
    findings = certificate_findings(guard_identities([identity for identity, _ in IDENTITIES]), records, NAMESPACE, now)
    statuses = {item["status"] for item in findings}
    return {"status": "INVALID" if "FAIL" in statuses else "ESCALATE" if "UNKNOWN" in statuses else "PASS",
            "findings": [{key: item[key] for key in ("code", "path", "status", "actual")}
                         for item in findings if item["status"] != "PASS"]}


def read_identities(kubeconfig=None):
    """Live, read-only: every lab Certificate's status, with the lab's admin context (or a given kubeconfig)."""
    from services.guard.adapters import read_certificate_status
    return read_certificate_status({"cluster_id": CLUSTER, "namespace": NAMESPACE},
                                   ["security-" + identity for identity, _ in IDENTITIES],
                                   context=CONTEXT, cluster_id=CLUSTER, kubeconfig=kubeconfig)


def live_recipe(lab):
    """The documented live recipe (docs/security.md, Certificate lifecycle); nothing runs here."""
    python = "python"
    probe = [python, "scripts/security/boutique_security.py", "probe", "--directory", str(lab)]
    k = ["kubectl", "--context", CONTEXT, "--namespace", NAMESPACE]
    return {
        "offline": [[python, "-m", "unittest", "tests.test_security_lab_context", "tests.test_guard_eligibility",
                     "tests.test_observer", "tests.test_planner", "tests.test_live_executor",
                     "tests.test_protected_recovery"],
                    [python, "scripts/security/cert_lifecycle.py", "pins", "--check"]],
        "start": [[python, "scripts/security/boutique_security.py", "start", "--directory", str(lab),
                   "--exit-after-test"]],
        "rotation": [k + ["get", "pods", "-o", "jsonpath={range .items[*]}{.metadata.name}{' '}"
                          "{.status.containerStatuses[*].restartCount}{'\\n'}{end}"],
                     k + ["get", "certificates.cert-manager.io", "-o", "jsonpath={range .items[*]}{.metadata.name}"
                          "{' '}{.status.revision}{' '}{.status.notAfter}{'\\n'}{end}"],
                     k + ["delete", "secret", "security-gateway", "security-frontend-client", "--wait=false"],
                     k + ["wait", "--for=condition=Ready", "certificates.cert-manager.io", "--all", "--timeout=300s"],
                     probe + ["--route", "shipping"]],
        "negatives": [[python, "scripts/security/cert_lifecycle.py", "negatives", "--directory", str(lab)],
                      *(probe + ["--identity", name] for name in NEGATIVE_IDENTITIES)],
        "escalation": [["kubectl", "--context", CONTEXT, "--namespace", CERT_MANAGER_NAMESPACE, "scale",
                        "deployment/cert-manager", "--replicas=0"],
                       [python, "scripts/security/cert_lifecycle.py", "identity"],
                       [python, "scripts/test_boutique_live.py", "--protected", "--security-run", str(lab),
                        "--protected-stage", "full", "--run-dir", "data/experimentation/runs/identity-expiring"],
                       ["kubectl", "--context", CONTEXT, "--namespace", CERT_MANAGER_NAMESPACE, "scale",
                        "deployment/cert-manager", "--replicas=1"]],
        "rollover": [[python, "scripts/security/cert_lifecycle.py", "rollover", "start", "--directory", str(lab)],
                     probe + ["--identity", OLD_CA_IDENTITY],
                     [python, "scripts/security/cert_lifecycle.py", "rollover", "switch", "--directory", str(lab)],
                     probe + ["--route", "shipping"],
                     [python, "scripts/security/cert_lifecycle.py", "rollover", "finish", "--directory", str(lab)],
                     probe + ["--identity", OLD_CA_IDENTITY]],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    pins = actions.add_parser("pins", help="Print the unresolved pins and the commands that resolve them")
    pins.add_argument("--check", action="store_true")
    plan = actions.add_parser("plan", help="Print the live recipe as JSON")
    plan.add_argument("--directory", type=Path, default=Path("data/sentinel/runs/boutique-security"))
    negative = actions.add_parser("negatives", help="Live: apply the refused test identities")
    negative.add_argument("--directory", type=Path, required=True)
    roll = actions.add_parser("rollover", help="Live: one CA rollover step")
    roll.add_argument("step", choices=("start", "switch", "finish"))
    roll.add_argument("--directory", type=Path, required=True)
    actions.add_parser("identity", help="Live, read-only: what the Guard's identity rule decides about each Certificate")
    args = parser.parse_args(argv)
    if args.action == "identity":
        report = identity_report(read_identities(), datetime.now(UTC))
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "PASS" else 1
    if args.action == "pins":
        values = load_pins()
        problems = unresolved_pins(values)
        for problem in problems:
            print("Unresolved: " + problem)
        print("Resolve with:\n  " + "\n  ".join(pin_commands(values)))
        return 1 if problems and args.check else 0
    if args.action == "plan":
        print(json.dumps(live_recipe(args.directory), indent=2))
        return 0
    directory = args.directory.resolve()
    session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    if session.get("context") != CONTEXT or session.get("certificates", {}).get("issuer") != "cert-manager":
        raise ValueError("The run folder is not a cert-manager security lab")
    if args.action == "negatives":
        print(json.dumps({"applied": negatives(directory, run)}))
    else:
        print(json.dumps({"step": args.step, "trust_bundle": rollover(directory, args.step, run)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
