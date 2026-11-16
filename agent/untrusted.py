"""Telemetry is hostile input: clean, template and quote it before a model reads it.

Container logs, Kubernetes event messages and incident signal details are
written by the workload. Anything that can make the workload print text can
put that text in front of the model. This module turns such text into bounded,
quoted data:

- clean() removes control and format characters (ANSI escapes, NUL, bidi
  controls, zero-width characters), applies Unicode NFKC, collapses
  whitespace and truncates with an explicit "[+N chars]" marker;
- template() masks the variable parts of a cleaned line: times, numbers,
  addresses, identifiers, paths and long quoted strings;
- suspicious() returns the ids of the instruction-like rules a text matches,
  never the text itself;
- quote() is what a prompt carries for one line: a template and a short
  excerpt marked untrusted, or, for a suspicious line, only its rule ids and
  a hash, so instruction text never reaches a model.

The rules are a curated pattern set, not a complete defence. They are one
layer next to the code-owned injection gate (services/planner/agent_provider.py),
rollback-only actions, the Guard and authenticated approval.
"""
import hashlib
import re
import unicodedata

# The prompt key that holds every log-derived string, so a model and a reader
# can tell quoted workload data from Sentinel's own instructions.
UNTRUSTED_KEY = "untrusted_telemetry_evidence"
EXCERPT_LIMIT = 160
LABEL_LIMIT = 80

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_BREAKS = frozenset("\t\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_REMOVED = frozenset({"Cc", "Cf", "Cs"})
_SPACE = re.compile(r"\s+")
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
_LINE_SPACE = re.compile(r"[^\S\n]+")
# ASCII controls as _views treats them: line breaks end a line, a tab is a
# space and every other control character is removed.
_ASCII_CONTROLS = {code: None for code in (*range(32), 127)}
_ASCII_CONTROLS.update({ord(character): "\n" for character in "\n\r\v\f"})
_ASCII_CONTROLS[ord("\t")] = " "

# Common Cyrillic and Greek letters that look like Latin ones. NFKC already
# folds full-width forms; this table covers the usual mixed-script spellings.
_CONFUSABLES = str.maketrans({
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y",
    "\u0445": "x", "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u0501": "d", "\u0261": "g",
    "\u04bb": "h", "\u04cf": "l", "\u0578": "n", "\u0410": "A", "\u0412": "B", "\u0415": "E",
    "\u041a": "K", "\u041c": "M", "\u041d": "H", "\u041e": "O", "\u0420": "P", "\u0421": "C",
    "\u0422": "T", "\u0425": "X", "\u0406": "I", "\u0408": "J", "\u0405": "S",
    "\u03b1": "a", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v", "\u03bf": "o",
    "\u03c1": "p", "\u03c4": "t", "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0397": "H",
    "\u0399": "I", "\u039a": "K", "\u039c": "M", "\u039d": "N", "\u039f": "O", "\u03a1": "P",
    "\u03a4": "T", "\u03a7": "X", "\u03a5": "Y", "\u0396": "Z"})

_QUALIFIER = r"(?:previous|prior|above|earlier|preceding|all|any|your|original|system|safety|every)"
_TARGET = (r"(?:instructions?|prompts?|rules|guidelines|directives|guardrails|context|messages?|"
           r"constraints|policies)")
_ACTION = r"(?:rollback|escalate|no_action)"
# Rule id -> patterns, matched on cleaned, casefolded text (see _views).
RULES = {
    "instruction_override": (
        r"\b(?:ignore|disregard|forget|override|bypass)\b(?:\W+\w+){0,3}?\W+" + _QUALIFIER
        + r"\b(?:\W+\w+){0,3}?\W+" + _TARGET + r"\b",
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+|everything\s+)?(?:of\s+)?(?:the\s+)?(?:above|previous|prior)\b",
        r"\b(?:new|updated|revised|real|actual|hidden)\s+(?:system\s+)?instructions?\b",
        r"\byou\s+(?:are|must|will|should)\s+now\b",
    ),
    "model_directive": (
        r"\b(?:answer|respond|reply|output|return|choose|select|pick|recommend|propose|decide|emit)\b"
        r"(?:\s+(?:with|only|just|exactly|the\s+action|action|now|immediately))*\s*[:\"'`(]*\s*" + _ACTION + r"\b",
        r"\b(?:correct|right|required|only|expected)\s+(?:action|decision|answer)\s+(?:is|=|:)\s*[\"'`]?"
        + _ACTION + r"\b",
    ),
    "role_marker": (
        r"^(?:\S+:\s+)?(?:\d{4}-\d{2}-\d{2}t\S*\s+)?[#>*\[(<|=-]*\s*(?:system|assistant|developer|user)"
        r"\s*[\])>|]*\s*:",
        r"^\s*#{1,6}\s*(?:system|assistant|instructions?)\b",
    ),
    "chat_template_token": (
        r"<\|[a-z_]{2,20}\|>|\[/?inst\]|<<\s*/?sys\s*>>|</?(?:system|assistant|think)>",
    ),
    "schema_forgery": (
        r"\b(?:baseline_id|template_sha256|based_on_resource_version|evidence_refs)\b",
        r"[\"']?\baction\b[\"']?\s*[:=]\s*[\"']?" + _ACTION + r"\b",
        r"\bact-[0-9a-f]{24}\b",
    ),
    "approval_claim": (
        r"\bpre-?\s?(?:authori[sz]ed|approved)\b",
        r"\b(?:approved|signed\s+off)\s+by\s+(?:the\s+)?(?:operator|admin(?:istrator)?|sre|on-?call|human|"
        r"security|owner|manager)\b",
        r"\b(?:operator|admin(?:istrator)?|human)\s+(?:has\s+)?(?:already\s+)?(?:approved|authori[sz]ed)\b",
        r"\b(?:no|without)\s+(?:operator\s+|human\s+)?approval\s+(?:is\s+)?(?:needed|required|necessary)\b",
        r"\bapproval\s+(?:is\s+)?(?:granted|not\s+(?:needed|required))\b",
    ),
    "prompt_structure": (
        r"possible\s+root\s+cause\s+(?:components?|reasons?)",
        r"\b(?:untrusted_telemetry_evidence|system_context|corroborated_onsets|required_fields|"
        r"number_of_failures)\b",
        r"\broot\s+cause\s+(?:component|reason|occurrence\s+datetime)\s*[\"']?\s*:",
    ),
}
_COMPILED = [(rule, re.compile(pattern, re.MULTILINE)) for rule, patterns in RULES.items() for pattern in patterns]

# Variable parts of a line, masked in order. Times go first, so a clock is not
# read as an IPv6 address, and placeholders hold no digits for later masks.
_MASKS = (
    (re.compile(r"\b\d{4}[-/]\d{2}[-/]\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:z|[+-]\d{2}:?\d{2})?)?",
                re.IGNORECASE), "<time>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<time>"),
    (re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://[^\s\"'<>]+", re.IGNORECASE), "<url>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE), "<uuid>"),
    (re.compile(r"\[?(?<![\w:])(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}(?![\w:])\]?"
                r"|\[?(?<![\w:])(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?"
                r"(?![\w:])\]?", re.IGNORECASE), "<ip>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"(?<![\w<>])(?:/[\w.@~+-]+){2,}/?|\b[a-z]:\\[^\s\"']+", re.IGNORECASE), "<path>"),
    (re.compile(r"\"[^\"]{17,}\"|'[^']{17,}'"), "\"<str>\""),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<num>"),
)


def scrub(text, limit=240):
    """Return (cleaned text, characters removed, truncated?) for one telemetry string."""
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")
    text = "" if text is None else str(text)
    without_escapes = _ANSI.sub("", text)
    removed = len(text) - len(without_escapes)
    kept = []
    for character in unicodedata.normalize("NFKC", without_escapes):
        if character in _BREAKS:
            kept.append(" ")
        elif unicodedata.category(character) in _REMOVED:
            removed += 1
        else:
            kept.append(character)
    cleaned = _SPACE.sub(" ", "".join(kept)).strip()
    if len(cleaned) <= limit:
        return cleaned, removed, False
    return cleaned[:limit] + f" [+{len(cleaned) - limit} chars]", removed, True


def clean(text, limit=240):
    """Cleaned, single-line, bounded text; see scrub()."""
    return scrub(text, limit)[0]


def template(text):
    """Mask the variable parts of a cleaned line, keeping its shape."""
    for pattern, placeholder in _MASKS:
        text = pattern.sub(placeholder, text)
    return text


def _views(text):
    """The text as the rules see it: cleaned per line, casefolded, homoglyphs folded.

    Format characters are removed rather than replaced, so a zero-width split
    word is joined again. A bidi override reverses how a line is displayed, so a
    text with one is also checked with each line reversed.
    """
    raw = "" if text is None else str(text)
    if raw.isascii():
        # Same result as the general path: NFKC changes no ASCII text, and the
        # only removable ASCII characters are controls (the common, fast case).
        folded = _ANSI.sub(" ", raw).translate(_ASCII_CONTROLS).lower()
    else:
        lines = []
        for character in unicodedata.normalize("NFKC", _ANSI.sub(" ", raw)):
            if character in "\n\r\v\f\x85\u2028\u2029":
                lines.append("\n")
            elif character == "\t":
                lines.append(" ")
            elif unicodedata.category(character) not in _REMOVED:
                lines.append(character)
        folded = "".join(lines).translate(_CONFUSABLES).casefold()
    folded = "\n".join(_LINE_SPACE.sub(" ", line).strip() for line in folded.split("\n"))
    views = [folded]
    if _BIDI.search(raw):
        views.append("\n".join(line[::-1] for line in folded.split("\n")))
    return views


def suspicious(text):
    """Sorted ids of the instruction-like rules the text matches; [] for ordinary telemetry."""
    found = set()
    for view in _views(text):
        found.update(rule for rule, pattern in _COMPILED if rule not in found and pattern.search(view))
    return sorted(found)


def sha256(text):
    return hashlib.sha256(("" if text is None else str(text)).encode("utf-8", "surrogatepass")).hexdigest()


def quote(text, limit=EXCERPT_LIMIT):
    """One telemetry string as prompt data, never as text a model could take as instructions.

    An ordinary line gives a masked template and a short cleaned excerpt. A
    suspicious line gives only its rule ids and the hash of the raw text, which
    finds it on disk; its words are withheld from every prompt.
    """
    rules = suspicious(text)
    if rules:
        return {"untrusted": True, "withheld": True, "suspected_injection_rules": rules, "sha256": sha256(text)}
    excerpt = clean(text, limit)
    return {"untrusted": True, "template": template(clean(text, 2 * limit)), "excerpt": excerpt}


def label(text):
    """A short cleaned name, such as a component or KPI read from telemetry."""
    return clean(text, LABEL_LIMIT)


def quote_signal(signal):
    """An incident signal for a prompt: identifiers kept, its reason and detail quoted."""
    return {"evidence_id": signal["evidence_id"], "source": label(signal["source"]), "at": signal["at"],
            "reason": quote(signal["reason"], LABEL_LIMIT), "detail": quote(signal["detail"])}


def prompt_incident(incident):
    """The incident as prompt context: trusted fields as they are, signals quoted as untrusted data."""
    trusted = {key: value for key, value in incident.items() if key != "signals"}
    return {"incident": trusted,
            UNTRUSTED_KEY: {"incident_signals": [quote_signal(signal) for signal in incident["signals"]]}}
