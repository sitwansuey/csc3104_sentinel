"""Pure sanitiser for evidence files copied out of run directories.

Secrets under sensitive keys are redacted to a short hash. Credential material
(PEM blocks, JWTs, kubeconfig key data, long base64 and known token shapes)
is a hard failure: the bundle is refused instead of partly cleaned. Absolute
home and repository paths and the local user names become placeholders, and
private IPv4 addresses (RFC 1918 and link-local, such as kind Pod IPs) become
<private-ip>; 127.0.0.1 is kept.
"""

from dataclasses import dataclass, field
import hashlib
import json
import re

SANITISER_VERSION = "5"
FILE_CAP_BYTES = 256 * 1024
BUNDLE_CAP_BYTES = 3 * 1024 * 1024
MODEL_IO_CAP = 4096

# Values under these keys are redacted. The kept keys name a Kubernetes Secret, not its content,
# or are Guard evaluation metric names (counts and rates), not credentials.
REDACT_KEY = re.compile(r"token|password|passwd|authorization|api[_-]?key|cookie|private[_-]?key|secret",
                        re.IGNORECASE)
KEEP_KEYS = frozenset({"secretName", "secretKeyRef", "secretRef",
                       "unsafe_non_authorization", "safe_non_authorization"})
# A number under a plural '...tokens' key is a model token count, not a secret.
COUNT_KEY = re.compile(r"tokens$", re.IGNORECASE)
# A command-line flag whose next argument is a secret, such as kubectl --token VALUE.
SECRET_FLAG = re.compile(r"--?[A-Za-z_-]*(?:token|password|passwd|api[_-]?key|secret)[A-Za-z_-]*", re.IGNORECASE)
MODEL_IO_KEYS = frozenset({"prompt", "prompts", "response", "responses", "messages", "raw_response", "completion"})

HARD_FAIL = (
    ("PEM block", re.compile(r"-----BEGIN [A-Z0-9 ]+-----")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("kubeconfig key data", re.compile(r"(client-key-data|client-certificate-data|certificate-authority-data)",
                                       re.IGNORECASE)),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("long base64", re.compile(r"[A-Za-z0-9+/]{512,}={0,2}")),
)
BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
# Inline 'key: value' text. The key may be quoted ("token": or 'api_key': in JSON or a Python
# repr, \"token\": in JSON escaped inside a log line). The value is a quoted string, an escaped
# quoted string, or a bare word that does not open an object or list.
_ESCAPED_VALUE = r"\\\"[^\"\r\n]*?\\\""
TEXT_SECRET = re.compile(r"(?i)\b((?:[a-z_-]*)(?:token|password|passwd|api[_-]?key|secret)[a-z_-]*)"
                         r"(\\?[\"']?\s*[:=]\s*)"
                         r"(\"[^\"]*\"|'[^']*'|" + _ESCAPED_VALUE +
                         r"|[^\s,;{}\[\]\"'\\](?:[^\s,;}\"'\\]|\\(?!\"))*)")
# HTTP header forms: the whole value after the colon is secret, whatever the scheme. A quoted
# value is taken whole; an empty or whitespace-only value is not a match.
HEADER_SECRET = re.compile(r"(?i)\b((?:proxy-)?authorization|set-cookie|cookie)(\\?[\"']?[ \t]*:[ \t]*)"
                           r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|" + _ESCAPED_VALUE +
                           r"|[^\s\"'\\](?:[^\r\n\"'\\]|\\(?!\"))*)")
_KEEP_INLINE = frozenset({"true", "false", "null", "none"})
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
HOME_PATTERNS = (
    re.compile(r"[A-Za-z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/)[^\\/\"'\s:;,]+", re.IGNORECASE),
    re.compile(r"(?<![\w.-])/home/[^/\"'\s:;,]+"),
    re.compile(r"(?<![\w.-])/Users/[^/\"'\s:;,]+"),
)

# RFC 1918 and link-local IPv4 addresses, not part of a longer dotted number. A port stays.
PRIVATE_IPV4 = re.compile(r"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168|169\.254)"
                          r"\.\d{1,3}\.\d{1,3}(?![\d]|\.\d)")


class SanitiseError(ValueError):
    """Credential material or an unsupported file: nothing may be written."""


@dataclass
class Context:
    repo_root: str = None
    home: str = None
    usernames: tuple = ()
    include_model_io: bool = False
    redactions: int = field(default=0)


def redacted(value):
    return "<redacted:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] + ">"


def check_text(text, name):
    """Raise SanitiseError when the raw text holds credential material."""
    for label, pattern in HARD_FAIL:
        if pattern.search(text):
            raise SanitiseError(f"{name}: refused, contains {label}")


def _path_variants(path):
    path = path.rstrip("\\/")
    forward = path.replace("\\", "/")
    back = forward.replace("/", "\\")
    return sorted({path, forward, back, back.replace("\\", "\\\\")}, key=len, reverse=True)


def rewrite_paths(text, context):
    """Replace repository, home and user-profile paths, then local user names."""
    for value, placeholder in ((context.repo_root, "<repo>"), (context.home, "<home>")):
        if value and len(value) > 3:
            for variant in _path_variants(value):
                text = re.sub(re.escape(variant), placeholder, text, flags=re.IGNORECASE)
    for pattern in HOME_PATTERNS:
        text = pattern.sub("<home>", text)
    for user in context.usernames:
        if user and len(user) >= 3:
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(user) + r"(?![A-Za-z0-9])", "<user>", text,
                          flags=re.IGNORECASE)
    return text


def _keep_inline(key, value):
    """Mirror _redact_value for text: keep resource names, flags, nulls and token counts."""
    if key in KEEP_KEYS or value.replace("\\\"", "").strip("\"'") == "" or value.lower() in _KEEP_INLINE:
        return True
    return COUNT_KEY.search(key) is not None and _NUMBER.fullmatch(value) is not None


def _string(value, context):
    """Rewrite paths and redact inline secrets in any text, JSON string values included."""
    def replace(match):
        # Keep the quotes of a quoted value so a JSON-shaped line stays well formed.
        context.redactions += 1
        secret, quote = match.group(3), ""
        for mark in ("\\\"", "\"", "'"):
            if len(secret) >= 2 * len(mark) and secret.startswith(mark) and secret.endswith(mark):
                secret, quote = secret[len(mark):-len(mark)], mark
                break
        return match.group(1) + match.group(2) + quote + redacted(secret) + quote

    def replace_text(match):
        return match.group(0) if _keep_inline(match.group(1), match.group(3)) else replace(match)
    value = HEADER_SECRET.sub(replace, value)
    value = TEXT_SECRET.sub(replace_text, value)
    value = rewrite_paths(value, context)
    value, count = BEARER.subn(lambda match: match.group(1) + " " + redacted(match.group(0)), value)
    context.redactions += count
    value, count = PRIVATE_IPV4.subn("<private-ip>", value)
    context.redactions += count
    return value


def _sensitive(key):
    return isinstance(key, str) and key not in KEEP_KEYS and bool(REDACT_KEY.search(key))


def _redact_value(value, key, context):
    """Redact anything under a sensitive key except null, flags and token counts."""
    if value is None or value == "" or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and COUNT_KEY.search(key):
        return value
    context.redactions += 1
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return redacted(text)


def _walk(value, context, key=None):
    if isinstance(value, dict):
        if "clusters" in value and "users" in value and isinstance(value.get("users"), list):
            raise SanitiseError("refused, kubeconfig document")
        # Kubernetes env lists and HTTP header lists carry the secret name in a sibling 'name'.
        pair_secret = _sensitive(value.get("name"))
        result = {}
        for name, item in value.items():
            if name in MODEL_IO_KEYS and not context.include_model_io:
                context.redactions += 1
                continue
            if name in MODEL_IO_KEYS and isinstance(item, str) and len(item) > MODEL_IO_CAP:
                item = item[:MODEL_IO_CAP] + "...<truncated>"
            if _sensitive(name):
                result[rewrite_paths(name, context)] = _redact_value(item, name, context)
            elif pair_secret and name == "value":
                result[name] = _redact_value(item, "value", context)
            else:
                result[rewrite_paths(name, context)] = _walk(item, context, name)
        return result
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            flag = index > 0 and isinstance(value[index - 1], str) and SECRET_FLAG.fullmatch(value[index - 1])
            result.append(_redact_value(item, "value", context) if flag else _walk(item, context, key))
        return result
    if isinstance(value, str):
        nested = _nested_json(value, context)
        return _string(value, context) if nested is None else nested
    return value


def _nested_json(value, context):
    """Walk JSON serialised inside a string, such as kubectl -o json stdout.

    Returns the re-serialised result only when the walk redacted something, so a string with
    nothing to hide keeps its original layout. Returns None for anything that is not a JSON
    object or list.
    """
    if value.lstrip()[:1] not in ("{", "["):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    probe = Context(context.repo_root, context.home, context.usernames, context.include_model_io)
    walked = _walk(parsed, probe)
    if not probe.redactions:
        return None
    context.redactions += probe.redactions
    return json.dumps(walked, ensure_ascii=False)


def _dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def sanitise_text(text, name, context):
    """Sanitise free text (logs, YAML, CSV): inline secrets are redacted."""
    return _string(text, context)


def sanitise_bytes(data, name, context):
    """Return (sanitised bytes, redaction count) or raise SanitiseError."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SanitiseError(f"{name}: refused, not UTF-8 text") from error
    check_text(text, name)
    before = context.redactions
    lower = name.lower()
    if lower.endswith(".json"):
        try:
            output = _dump(_walk(json.loads(text), context))
        except ValueError as error:
            raise SanitiseError(f"{name}: refused, invalid JSON") from error
    elif lower.endswith(".jsonl"):
        lines = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                lines.append(json.dumps(_walk(json.loads(line), context), ensure_ascii=False, allow_nan=False))
            except ValueError:
                lines.append(sanitise_text(line, name, context))
        output = "\n".join(lines) + "\n"
    else:
        output = sanitise_text(text.replace("\r\n", "\n"), name, context)
    check_text(output, name)
    return output.encode("utf-8"), context.redactions - before
