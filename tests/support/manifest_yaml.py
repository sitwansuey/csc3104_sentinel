"""A strict reader for the small YAML subset used by deploy/sentinel manifests.

The project has no YAML dependency, so the manifest tests read Kubernetes
manifests with this reader. It understands block mappings and sequences
(including "- key: value" items and lists at their key's indentation), plain,
single- and double-quoted scalars, one-line flow sequences such as
["apps"], the empty flow mapping {} (podSelector: {}, emptyDir: {}),
literal block scalars (| and |-, also as a "- |" list item), full-line comments and "---"
document separators. Anything else (tabs, anchors, aliases, tags, other flow
mappings, folded scalars, trailing comments after quotes) raises
ManifestYamlError instead of being read wrongly. It is a test helper, not a
YAML implementation: kubectl remains the authority on what a manifest means.
"""

import json
import re


class ManifestYamlError(ValueError):
    """The text uses YAML this reader does not support, or is malformed."""


_KEY = re.compile(r"(?P<key>[A-Za-z0-9_./-]+):(?:[ ](?P<value>.*))?\Z")
_INT = re.compile(r"-?(0|[1-9][0-9]*)\Z")
_UNSUPPORTED_START = ("&", "*", "!", "{", ">", "%", "@", "`", "?")


def load_all(text):
    """Every non-empty document in text, in order."""
    if "\t" in text:
        raise ManifestYamlError("Tabs are not supported")
    documents, current = [], []
    for raw in text.splitlines():
        if raw.rstrip() == "---":
            documents.append(current)
            current = []
        elif raw.startswith("---") or raw.startswith("..."):
            raise ManifestYamlError("Unsupported document marker: " + raw)
        else:
            current.append(raw.rstrip())
    documents.append(current)
    loaded = []
    for lines in documents:
        reader = _Reader(lines)
        position = reader.skip(0)
        if position == len(lines):
            continue
        if reader.indent(position) != 0:
            raise ManifestYamlError("A document must start at column 0")
        value, position = reader.node(position, 0)
        if reader.skip(position) != len(lines):
            raise ManifestYamlError(f"Unparsed content at document line {reader.skip(position) + 1}")
        loaded.append(value)
    return loaded


class _Reader:
    def __init__(self, lines):
        self.lines = list(lines)

    @staticmethod
    def _blank(line):
        stripped = line.strip()
        return not stripped or stripped.startswith("#")

    def skip(self, position):
        while position < len(self.lines) and self._blank(self.lines[position]):
            position += 1
        return position

    def indent(self, position):
        line = self.lines[position]
        return len(line) - len(line.lstrip(" "))

    def content(self, position):
        return self.lines[position].strip()

    @staticmethod
    def _is_item(content):
        return content == "-" or content.startswith("- ")

    def node(self, position, indent):
        position = self.skip(position)
        if position == len(self.lines) or self.indent(position) < indent:
            return None, position
        indent = self.indent(position)
        if self._is_item(self.content(position)):
            return self.sequence(position, indent)
        return self.mapping(position, indent)

    def mapping(self, position, indent):
        result = {}
        while True:
            position = self.skip(position)
            if position == len(self.lines) or self.indent(position) < indent:
                return result, position
            if self.indent(position) > indent:
                raise ManifestYamlError(f"Unexpected indentation at line {position + 1}")
            content = self.content(position)
            if self._is_item(content):
                return result, position
            match = _KEY.match(content)
            if not match:
                raise ManifestYamlError(f"Not a supported mapping entry at line {position + 1}: {content}")
            key, value = match.group("key"), (match.group("value") or "").strip()
            if key in result:
                raise ManifestYamlError(f"Duplicate key {key!r} at line {position + 1}")
            if value in ("|", "|-"):
                result[key], position = self.literal(position + 1, indent, keep=value == "|")
            elif value:
                result[key], position = scalar(value), position + 1
            else:
                following = self.skip(position + 1)
                if (following < len(self.lines) and self.indent(following) == indent
                        and self._is_item(self.content(following))):
                    result[key], position = self.sequence(following, indent)
                elif following < len(self.lines) and self.indent(following) > indent:
                    result[key], position = self.node(following, indent + 1)
                else:
                    result[key], position = None, position + 1

    def sequence(self, position, indent):
        result = []
        while True:
            position = self.skip(position)
            if position == len(self.lines) or self.indent(position) != indent:
                if position < len(self.lines) and self.indent(position) > indent:
                    raise ManifestYamlError(f"Unexpected indentation at line {position + 1}")
                return result, position
            content = self.content(position)
            if not self._is_item(content):
                return result, position
            rest = content[1:]
            if not rest.strip():
                value, position = self.node(position + 1, indent + 1)
            elif rest.strip() in ("|", "|-"):
                # "- |" is a literal block as a list item (kind's kubeadmConfigPatches).
                value, position = self.literal(position + 1, indent, keep=rest.strip() == "|")
            else:
                offset = indent + 1 + len(rest) - len(rest.lstrip(" "))
                rest = rest.strip()
                if _KEY.match(rest):
                    # "- key: value" opens a mapping whose entries sit at the key's column.
                    self.lines[position] = " " * offset + rest
                    value, position = self.mapping(position, offset)
                else:
                    value, position = scalar(rest), position + 1
            result.append(value)

    def literal(self, position, indent, *, keep):
        block, block_indent = [], None
        while position < len(self.lines):
            line = self.lines[position]
            if line.strip():
                current = len(line) - len(line.lstrip(" "))
                if current <= indent:
                    break
                if block_indent is None:
                    block_indent = current
                if current < block_indent:
                    raise ManifestYamlError(f"Literal block is less indented at line {position + 1}")
                block.append(line[block_indent:])
            else:
                block.append("")
            position += 1
        while block and not block[-1]:
            block.pop()
        if not block:
            raise ManifestYamlError(f"Empty literal block before line {position + 1}")
        return "\n".join(block) + ("\n" if keep else ""), position


def scalar(text):
    """One scalar or one-line flow sequence."""
    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            raise ManifestYamlError("Unterminated double-quoted scalar: " + text)
        try:
            value = json.loads(text)
        except ValueError:
            raise ManifestYamlError("Unsupported double-quoted scalar: " + text) from None
        if not isinstance(value, str):
            raise ManifestYamlError("Unsupported double-quoted scalar: " + text)
        return value
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2 or "'" in text[1:-1].replace("''", ""):
            raise ManifestYamlError("Unsupported single-quoted scalar: " + text)
        return text[1:-1].replace("''", "'")
    if text == "{}":
        return {}
    if text.startswith("["):
        if not text.endswith("]"):
            raise ManifestYamlError("Unsupported flow sequence: " + text)
        return [scalar(item) for item in _flow_items(text[1:-1])]
    if text.startswith(_UNSUPPORTED_START) or text.startswith(("|", "-")) and not _INT.match(text):
        raise ManifestYamlError("Unsupported YAML syntax: " + text)
    if " #" in text:
        text = text.split(" #", 1)[0].rstrip()
    if ": " in text or text.endswith(":"):
        raise ManifestYamlError("A plain scalar may not contain ': ' here: " + text)
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~"):
        return None
    if _INT.match(text):
        return int(text)
    return text


def _flow_items(text):
    items, current, quote = [], "", None
    for character in text:
        if quote:
            current += character
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
            current += character
        elif character == ",":
            items.append(current.strip())
            current = ""
        elif character in "[]{}":
            raise ManifestYamlError("Nested flow collections are not supported")
        else:
            current += character
    if quote:
        raise ManifestYamlError("Unterminated quote in flow sequence")
    if current.strip():
        items.append(current.strip())
    elif items:
        raise ManifestYamlError("Empty flow sequence item")
    if any(not item for item in items):
        raise ManifestYamlError("Empty flow sequence item")
    return items
