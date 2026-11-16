"""Read the marker-facing instructions: includes, tagged command blocks and their checks.

The single source is docs/instructions/GROUP05_INSTRUCTIONS.md. Every PowerShell
or bash block in it is a command block and carries a tag in its info string:

    ```powershell rehearse=B2 os=windows timeout=3600 needs=network,cluster

  rehearse   step id: one capital letter (the section) and a number; unique,
             and numbered upwards within each letter in document order
  os         windows (PowerShell blocks) or linux (bash blocks)
  timeout    the rehearsal time limit in seconds (1 to 14400)
  needs      optional: what the step needs besides the repository; see NEEDS
  optional   optional: yes for steps that only run when something went wrong

A line '<!-- include: docs/setup.md#required-tools -->' is replaced by the body of
that heading's section, so shared text is written once. Included text must not
contain command blocks or further includes.

check() verifies the tags and, for each command, that every script it names
exists and that every flag passed to that script is declared: in the param()
block of a .ps1 file, in the case labels of a .sh file or the argparse options of
the Python script it runs, or in a .py file's add_argument calls (following one
import_module delegation, as scripts/test_boutique_live.py does). The check reads
files only; it runs nothing. Standard library only.
"""

from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_doc_links import slug  # noqa: E402

SOURCE = "docs/instructions/GROUP05_INSTRUCTIONS.md"
COMMAND_LANGUAGES = {"powershell": "windows", "bash": "linux"}
NEEDS = ("network", "docker", "cluster", "model", "human")
ATTRIBUTES = ("rehearse", "os", "timeout", "needs", "optional")
STEP_ID = re.compile(r"^(?P<letter>[A-Z])(?P<number>[1-9][0-9]*)$")
FENCE_OPEN = re.compile(r"^(?P<indent> {0,3})(?P<fence>```+|~~~+)(?P<info>.*)$")
HEADING = re.compile(r"^ {0,3}(?P<marks>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
INCLUDE = re.compile(r"^\s*<!--\s*include:\s*(?P<path>[^#\s]+)#(?P<anchor>[^\s]+)\s*-->\s*$")
SCRIPT = re.compile(r"^(?:\./|\.\\)?(?P<path>[A-Za-z0-9_.\-]+(?:[\\/][A-Za-z0-9_.\-]+)*\.(?:ps1|sh|py|bat))$")
SEPARATORS = frozenset({"|", ";", "&&", "||"})
MAX_TIMEOUT = 14400


class SourceError(ValueError):
    """The instructions source cannot be read or an include cannot be resolved."""


@dataclass
class Block:
    """One fenced block of the source, with its tag when it is a command block."""

    line: int
    language: str
    info: str
    text: str
    section: str
    attributes: dict = field(default_factory=dict)

    @property
    def step(self):
        return self.attributes.get("rehearse", "")

    @property
    def os(self):
        return self.attributes.get("os", "")

    @property
    def timeout(self):
        value = self.attributes.get("timeout", "")
        return int(value) if value.isdigit() else 0

    @property
    def needs(self):
        value = self.attributes.get("needs", "")
        return tuple(item for item in value.split(",") if item)

    @property
    def optional(self):
        return self.attributes.get("optional") == "yes"

    @property
    def is_command(self):
        return self.language in COMMAND_LANGUAGES

    def as_dict(self):
        return {"step": self.step, "os": self.os, "timeout": self.timeout, "needs": list(self.needs),
                "optional": self.optional, "language": self.language, "section": self.section,
                "line": self.line, "commands": self.text}


def read(path):
    return Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")


def parse_info(info):
    """Split an info string into its language and key=value attributes; bad pairs are kept as errors."""
    words = info.split()
    language = words[0].lower() if words else ""
    attributes, errors = {}, []
    for word in words[1:]:
        key, sep, value = word.partition("=")
        if not sep or not value:
            errors.append(f"tag '{word}' is not key=value")
        elif key not in ATTRIBUTES:
            errors.append(f"unknown tag '{key}'")
        elif key in attributes:
            errors.append(f"tag '{key}' given twice")
        else:
            attributes[key] = value
    return language, attributes, errors


def blocks(text):
    """Return (blocks, errors) for every fenced block, numbered by source line."""
    lines = text.split("\n")
    found, errors, section, index = [], [], "", 0
    while index < len(lines):
        line = lines[index]
        heading = HEADING.match(line)
        if heading:
            section = heading.group("text")
        opened = FENCE_OPEN.match(line)
        if not opened:
            index += 1
            continue
        fence, start = opened.group("fence"), index
        body, index = [], index + 1
        while index < len(lines) and not re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$",
                                                  lines[index]):
            body.append(lines[index])
            index += 1
        if index >= len(lines):
            errors.append(f"line {start + 1}: unclosed code fence")
        index += 1
        language, attributes, problems = parse_info(opened.group("info").strip())
        errors.extend(f"line {start + 1}: {problem}" for problem in problems)
        found.append(Block(start + 1, language, opened.group("info").strip(), "\n".join(body), section, attributes))
    return found, errors


def section_body(path, anchor, root=ROOT):
    """The text under the heading whose GitHub anchor is 'anchor', up to the next heading of its level or above."""
    target = Path(root, path)
    if not target.is_file():
        raise SourceError(f"include {path}#{anchor}: file not found")
    lines = read(target).split("\n")
    seen, level, collected, in_fence = {}, None, [], None
    for line in lines:
        fence = FENCE_OPEN.match(line)
        heading = None if in_fence else HEADING.match(line)
        if heading:
            base = slug(heading.group("text"))
            count = seen.get(base, 0)
            seen[base] = count + 1
            name = base if count == 0 else f"{base}-{count}"
            depth = len(heading.group("marks"))
            if level is not None and depth <= level:
                break
            if level is None and name == anchor:
                level = depth
                continue
        if level is not None:
            collected.append(line)
        if fence:
            marker = fence.group("fence")[0]
            in_fence = None if in_fence == marker else (in_fence or marker)
    if level is None:
        raise SourceError(f"include {path}#{anchor}: no such heading")
    return "\n".join(collected).strip("\n")


def resolve(text, root=ROOT):
    """Replace every include marker with the section it names."""
    output = []
    for line in text.split("\n"):
        marker = INCLUDE.match(line)
        if not marker:
            output.append(line)
            continue
        body = section_body(marker.group("path"), marker.group("anchor"), root)
        if INCLUDE.search(body) or blocks(body)[0]:
            raise SourceError(f"include {marker.group('path')}#{marker.group('anchor')}: included text holds "
                              "a code block or another include; write commands in the instructions source")
        output.append(body)
    return "\n".join(output)


def load(root=ROOT, source=SOURCE):
    """Return (source text, resolved text); a missing file or a bad include raises SourceError."""
    path = Path(root, source)
    if not path.is_file():
        raise SourceError(f"{source} is missing")
    text = read(path)
    return text, resolve(text, root)


def commands(block):
    """Logical command lines of a block: continuation lines joined, comments and blank lines dropped."""
    joined, pending = [], ""
    continuation = "`" if block.language == "powershell" else "\\"
    for raw in block.text.split("\n"):
        line = raw.rstrip()
        if not pending and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith(continuation):
            pending += line[:-1] + " "
            continue
        joined.append((pending + line).strip())
        pending = ""
    if pending.strip():
        joined.append(pending.strip())
    return joined


def tokens(command, language):
    try:
        return shlex.split(command, posix=language != "powershell")
    except ValueError:
        return command.split()


def _unquote(token):
    return token[1:-1] if len(token) > 1 and token[0] == token[-1] and token[0] in "'\"" else token


def _python_options(path, root, seen):
    """Option strings declared with add_argument in a Python script, following import_module once per file."""
    if path in seen or not path.is_file():
        return set()
    seen.add(path)
    text = path.read_text(encoding="utf-8")
    options = set()
    for call in re.finditer(r"add_argument\(([^)]*)", text):
        options.update(re.findall(r"""["'](-{1,2}[A-Za-z][\w-]*)["']""", call.group(1)))
    for module in re.findall(r"""import_module\(\s*["']([\w.]+)["']\s*\)""", text):
        options |= _python_options(Path(root, *module.split(".")).with_suffix(".py"), root, seen)
    return options | {"-h", "--help"}


def _powershell_parameters(path):
    text = path.read_text(encoding="utf-8")
    start = re.search(r"(?im)^\s*param\s*\(", text)
    if not start:
        return set()
    depth, index = 0, start.end() - 1
    for index in range(start.end() - 1, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            break
    return {name.lower() for name in re.findall(r"\$(\w+)", text[start.end():index])}


def _shell_options(path, root):
    text = path.read_text(encoding="utf-8")
    options = set(re.findall(r"^\s*(--[\w-]+)\)", text, re.MULTILINE))
    for script in re.findall(r"(scripts/[\w/.-]+\.py)", text):
        options |= _python_options(Path(root, script), root, set())
    return options


def declared_flags(script, root=ROOT):
    """(kind, declared flags) for a repository script; kind is 'powershell', 'shell' or 'python'."""
    path = Path(root, script)
    if script.endswith(".ps1"):
        return "powershell", _powershell_parameters(path)
    if script.endswith(".sh"):
        return "shell", _shell_options(path, root)
    if script.endswith(".py"):
        return "python", _python_options(path, root, set())
    return "other", set()


def command_problems(block, root=ROOT):
    """Scripts that do not exist and flags a script does not declare, for each command in a block."""
    problems = []
    for command in commands(block):
        words = [_unquote(word) for word in tokens(command, block.language)]
        script, kind, declared = None, None, set()
        for word in words:
            if word in SEPARATORS:
                script = None
                continue
            match = SCRIPT.match(word)
            if match and ("/" in word or "\\" in word):
                script = match.group("path").replace("\\", "/")
                if not Path(root, script).is_file():
                    problems.append(f"{block.step}: script {script} does not exist")
                    script = None
                    continue
                kind, declared = declared_flags(script, root)
                continue
            if script is None or not word.startswith("-") or re.match(r"^-\d", word):
                continue
            if kind == "powershell":
                name = word[1:].split(":", 1)[0].lower()
                if name not in declared:
                    problems.append(f"{block.step}: {script} has no parameter -{word[1:].split(':', 1)[0]}")
            elif kind in ("shell", "python"):
                name = word.split("=", 1)[0]
                if name not in declared:
                    problems.append(f"{block.step}: {script} does not accept {name}")
    return problems


def check(root=ROOT, source=SOURCE):
    """Return every problem with the instructions source; an empty list means it is consistent."""
    try:
        text, _resolved = load(root, source)
    except SourceError as error:
        return [str(error)]
    found, problems = blocks(text)
    seen, last = {}, {}
    for block in found:
        where = f"line {block.line}"
        tagged = "rehearse" in block.attributes
        if not block.is_command:
            if block.attributes:
                problems.append(f"{where}: only powershell and bash blocks take rehearsal tags")
            continue
        if not tagged:
            problems.append(f"{where}: {block.language} block has no rehearse= tag")
            continue
        step = STEP_ID.match(block.step)
        if not step:
            problems.append(f"{where}: step id '{block.step}' is not a capital letter and a number")
        elif block.step in seen:
            problems.append(f"{where}: step {block.step} repeats line {seen[block.step]}")
        else:
            seen[block.step] = block.line
            letter, number = step.group("letter"), int(step.group("number"))
            if number <= last.get(letter, 0):
                problems.append(f"{where}: step {block.step} comes after {letter}{last[letter]}; number upwards")
            last[letter] = max(number, last.get(letter, 0))
        if block.os != COMMAND_LANGUAGES[block.language]:
            problems.append(f"{where}: {block.language} block needs os={COMMAND_LANGUAGES[block.language]}")
        if not 1 <= block.timeout <= MAX_TIMEOUT:
            problems.append(f"{where}: timeout must be whole seconds from 1 to {MAX_TIMEOUT}")
        unknown = [item for item in block.needs if item not in NEEDS]
        if unknown:
            problems.append(f"{where}: unknown needs {', '.join(unknown)}; use {', '.join(NEEDS)}")
        if "optional" in block.attributes and block.attributes["optional"] != "yes":
            problems.append(f"{where}: optional takes only 'yes'")
        if not commands(block):
            problems.append(f"{where}: step {block.step} has no command")
        problems.extend(command_problems(block, root))
    return problems


def steps(root=ROOT, source=SOURCE, os_name=None, include_optional=True):
    """The tagged command blocks in document order, optionally for one OS and without optional steps."""
    text, _resolved = load(root, source)
    found, _errors = blocks(text)
    return [block for block in found if block.is_command and block.step
            and (os_name is None or block.os == os_name) and (include_optional or not block.optional)]
