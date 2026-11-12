"""The incident replay corpus: layout, safety scan and loader (roadmap P4.04).

A corpus folder (evidence/replay/v1) holds one folder per case:

  cases/<case_id>/case.json      label-free metadata: provenance, source bundle,
                                 what was reconstructed, the clean twin
  cases/<case_id>/inputs/        everything a decision policy may read
  cases/<case_id>/recorded/      what the live run produced (captured agent
                                 cases only): proposal, Guard verdict, diagnosis
  cases/<case_id>/expected.json  the labels, beside inputs/ and never inside it

corpus-manifest.json lists every file with its SHA-256. Its own SHA-256 is the
corpus identity a replay run is bound to, so a resumed run refuses a changed
corpus. The name is not MANIFEST.json on purpose: that name marks an evidence
bundle, and scripts/evidence/summarize.py would look for this folder in
evidence/index.json.

Only allowlisted names are ever written (CASE_FILES). scan() walks the whole
tree and refuses any other file, anything named like a kubeconfig, a database
or a credential, SQLite bytes, and private key, token or kubeconfig text, so a
corpus that picked up such a file cannot be loaded or committed.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

SCHEMA = "sentinel-replay-corpus-v1"
CASE_SCHEMA = "sentinel-replay-case-v1"
EXPECTED_SCHEMA = "sentinel-replay-expected-v1"
MANIFEST = "corpus-manifest.json"
PROVENANCES = ("captured", "derived", "adversarial")
# Every file a corpus may hold, relative to a case folder. Anything else fails scan().
CASE_FILES = (
    "case.json", "expected.json",
    "inputs/incident.json", "inputs/policy.json", "inputs/baseline.json", "inputs/planning-state.json",
    "inputs/live-state.json", "inputs/dependency-finding.json",
    "inputs/observations/0001-observation.json", "inputs/observations/0002-observation.json",
    "recorded/proposal.json", "recorded/guard.json", "recorded/diagnosis.json", "recorded/decision.json",
)
TOP_FILES = (MANIFEST, "README.md")
# Keys a label file holds. None of them may appear as a key anywhere under inputs/.
LABEL_KEYS = frozenset({"expected", "correct", "acceptable", "unsafe", "scenario", "review",
                        "expected_component", "guard_on_rollback"})
WRITE_ACTIONS = frozenset({"ROLLBACK", "RESTART", "SCALE"})
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,95}")
# Names that must never be in a corpus, whatever folder they are in.
_FORBIDDEN_NAME = re.compile(
    r"kubeconfig|\.kube|credential|secret|token|password|\.sqlite|\.sqlite3|\.db$|\.db-|\.pem$|\.key$|\.crt$"
    r"|\.p12$|\.pfx$|id_rsa|id_ed25519|^pki$|\.env$", re.IGNORECASE)
# Content that must never be in a corpus file: SQLite pages, PEM blocks, kubeconfig key data, tokens.
_FORBIDDEN_TEXT = (
    (re.compile(rb"SQLite format 3\x00"), "SQLite database bytes"),
    (re.compile(rb"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----"), "a PEM key or certificate"),
    (re.compile(rb"client-(?:key|certificate)-data|certificate-authority-data"), "kubeconfig key data"),
    (re.compile(rb"\"(?:token|password|client_secret|api_key)\"\s*:\s*\"[^\"<]"), "a credential value"),
    (re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "a JWT"),
)


class CorpusError(ValueError):
    """The corpus is unsafe, incomplete or does not match its manifest."""


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    """Write canonical, byte-stable JSON (LF line endings on every OS)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scan(root):
    """Problems with a corpus tree; empty when every file is allowlisted and clean.

    The walk is recursive and includes hidden files: a stray .executor-kubeconfig.json
    or planner.db anywhere in the tree is reported.
    """
    root = Path(root)
    problems = []
    for path in sorted(root.rglob("*")):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if any(_FORBIDDEN_NAME.search(part) for part in relative.parts):
            problems.append(f"{relative}: forbidden name (kubeconfig, database or credential)")
            continue
        if path.is_dir():
            continue
        parts = relative.parts
        allowed = (str(relative) in TOP_FILES or (
            len(parts) >= 3 and parts[0] == "cases" and _CASE_ID.fullmatch(parts[1])
            and "/".join(parts[2:]) in CASE_FILES))
        if not allowed:
            problems.append(f"{relative}: not an allowlisted corpus file")
            continue
        data = path.read_bytes()
        for pattern, what in _FORBIDDEN_TEXT:
            if pattern.search(data):
                problems.append(f"{relative}: contains {what}")
    return problems


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def label_leaks(case_folder):
    """Label keys found under a case's inputs/ (always empty for a valid case)."""
    leaks = []
    for path in sorted((Path(case_folder) / "inputs").rglob("*.json")):
        found = sorted(set(_keys(read_json(path))) & LABEL_KEYS)
        if found:
            leaks.append(f"{path.name}: {', '.join(found)}")
    return leaks


def manifest_sha256(root):
    """The corpus identity: the SHA-256 of its manifest file."""
    path = Path(root) / MANIFEST
    if not path.is_file():
        raise CorpusError(f"{MANIFEST} is missing in {root}")
    return sha256_file(path)


def verify(root):
    """Scan the tree and check every file against the manifest; return the manifest."""
    root = Path(root)
    problems = scan(root)
    if problems:
        raise CorpusError("Unsafe corpus: " + "; ".join(problems[:10]))
    manifest = read_json(root / MANIFEST)
    if manifest.get("schema") != SCHEMA:
        raise CorpusError(f"{MANIFEST}: unknown schema")
    listed = set()
    for case in manifest.get("cases", []):
        for relative, digest in case.get("files", {}).items():
            path = root / "cases" / case["case_id"] / relative
            listed.add(path.relative_to(root).as_posix())
            if not path.is_file() or sha256_file(path) != digest:
                raise CorpusError(f"cases/{case['case_id']}/{relative} is missing or does not match the manifest")
    present = {path.relative_to(root).as_posix() for path in root.rglob("*")
               if path.is_file() and path.name not in TOP_FILES}
    if present - listed:
        raise CorpusError("Files not in the manifest: " + ", ".join(sorted(present - listed)[:5]))
    for case in manifest.get("cases", []):
        leaks = label_leaks(root / "cases" / case["case_id"])
        if leaks:
            raise CorpusError(f"{case['case_id']}: label keys under inputs/: " + "; ".join(leaks))
    return manifest


def load_case(root, case_id):
    """One case's metadata, inputs and recorded outputs (never its labels)."""
    folder = Path(root) / "cases" / case_id
    meta = read_json(folder / "case.json")
    if meta.get("schema") != CASE_SCHEMA or meta.get("case_id") != case_id:
        raise CorpusError(f"{case_id}: case.json is not a {CASE_SCHEMA} record for this case")
    inputs = {path.stem: read_json(path) for path in sorted((folder / "inputs").glob("*.json"))}
    for name in ("incident", "policy", "baseline", "planning-state", "live-state", "dependency-finding"):
        if name not in inputs:
            raise CorpusError(f"{case_id}: inputs/{name}.json is missing")
    recorded = {path.stem: read_json(path) for path in sorted((folder / "recorded").glob("*.json"))}
    return {"case_id": case_id, "meta": meta, "inputs": inputs, "recorded": recorded,
            "observations": folder / "inputs" / "observations"}


# Phrases that state an expected answer; none may appear in retrieval sources or a prompt.
LABEL_PHRASES = ("correct action", "expected action", "acceptable action", "unsafe action", "pre-registered",
                 "expected_component", "guard_on_rollback", "rollback_correct", "rollback_incorrect")


def label_terms(root):
    """Every label value in a corpus that would leak an answer: ids, names, classes and phrases.

    Scenario ids (R1, N1, ...) are matched as whole words. Case ids, variants and
    attack ids are corpus metadata a policy never reads, so they must not
    appear in its context either.
    """
    root = Path(root)
    terms = set(LABEL_PHRASES)
    catalog = read_json(Path(__file__).resolve().parents[2] / "experiments/boutique/catalog.json")
    terms.update(item["name"] for item in catalog["scenarios"])
    for case in read_json(root / MANIFEST)["cases"]:
        expected = load_expected(root, case["case_id"])
        meta = read_json(root / "cases" / case["case_id"] / "case.json")
        terms.update({case["case_id"], expected["scenario"], expected["class"]})
        terms.update(value for value in (expected.get("variant"), (meta.get("attack") or {}).get("id")) if value)
    return sorted(terms)


def contamination(texts, terms):
    """[(source, term)] for every label term found in the given {source: text}; case-insensitive."""
    patterns = [(term, re.compile(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", re.IGNORECASE)) for term in terms]
    return sorted((source, term) for source, text in texts.items() for term, pattern in patterns
                  if pattern.search(text))


def load_expected(root, case_id):
    expected = read_json(Path(root) / "cases" / case_id / "expected.json")
    if expected.get("schema") != EXPECTED_SCHEMA or expected.get("case_id") != case_id:
        raise CorpusError(f"{case_id}: expected.json is not a {EXPECTED_SCHEMA} record for this case")
    for key in ("correct", "acceptable", "unsafe"):
        if not isinstance(expected.get(key), list):
            raise CorpusError(f"{case_id}: expected.{key} must be a list")
    return deepcopy(expected)
