"""A small JSON Schema checker for model output.

Ollama's format grammar is a request, not a guarantee. Model results are
checked again here before any caller sees them. Only the keywords used by this
project are supported; any other keyword is refused so a new schema feature
cannot be silently ignored. Errors name the JSON path and never echo values or
model-chosen key names.
"""
import math

MAX_DEPTH = 32
ANNOTATIONS = {"description", "title"}
KEYWORDS = {"type", "properties", "required", "additionalProperties", "enum", "minimum", "maximum",
            "minItems", "maxItems", "minProperties", "maxProperties", "minLength", "maxLength",
            "items", "$ref", "$defs"} | ANNOTATIONS
TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _is_type(value, name):
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return type(value) is bool
    if name == "null":
        return value is None
    if name == "integer":
        return type(value) is int or type(value) is float and math.isfinite(value) and value.is_integer()
    return type(value) in (int, float) and math.isfinite(value)


def _fail(path, problem):
    raise ValueError(f"Model output does not match the schema at {path}: {problem}")


def _resolve(schema, root, path):
    reference = schema["$ref"]
    prefix = "#/$defs/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise ValueError(f"Unsupported schema reference at {path}")
    target = root.get("$defs", {}).get(reference[len(prefix):])
    if not isinstance(target, dict):
        raise ValueError(f"Unknown schema reference at {path}")
    return target


def check_schema(schema, root=None, path="$", depth=0):
    """Refuse a schema that uses keywords this checker would not enforce."""
    root = schema if root is None else root
    if depth > MAX_DEPTH:
        raise ValueError("Schema is nested too deeply")
    if not isinstance(schema, dict):
        raise ValueError(f"Schema at {path} must be an object")
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise ValueError(f"Unsupported schema keyword at {path}: {sorted(unknown)[0]}")
    if "$defs" in schema and schema is not root:
        raise ValueError(f"$defs is only supported at the schema root ({path})")
    if "$ref" in schema:
        if set(schema) - ANNOTATIONS - {"$ref"}:
            raise ValueError(f"$ref cannot be combined with other keywords at {path}")
        _resolve(schema, root, path)
    if "type" in schema and schema["type"] not in TYPES:
        raise ValueError(f"Unsupported schema type at {path}")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError(f"Schema enum at {path} must be a non-empty list")
    if "required" in schema and (not isinstance(schema["required"], list)
                                 or not all(isinstance(k, str) for k in schema["required"])):
        raise ValueError(f"Schema required at {path} must be a list of names")
    for key in ("minimum", "maximum"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError(f"Schema {key} at {path} must be a finite number")
    for key in ("minItems", "maxItems", "minProperties", "maxProperties", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError(f"Schema {key} at {path} must be a non-negative integer")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(f"Schema properties at {path} must be an object")
    for name, child in properties.items():
        check_schema(child, root, f"{path}.{name}", depth + 1)
    extra = schema.get("additionalProperties", True)
    if type(extra) is not bool:
        check_schema(extra, root, path + ".*", depth + 1)
    if "items" in schema:
        check_schema(schema["items"], root, path + "[]", depth + 1)
    if schema is root:
        definitions = schema.get("$defs", {})
        if not isinstance(definitions, dict):
            raise ValueError("Schema $defs must be an object")
        for name, child in definitions.items():
            check_schema(child, root, f"$defs.{name}", depth + 1)


def validate(value, schema, root=None, path="$", depth=0):
    """Raise ValueError naming the first path where value breaks schema."""
    root = schema if root is None else root
    if depth > MAX_DEPTH:
        _fail(path, "nested too deeply")
    if "$ref" in schema:
        return validate(value, _resolve(schema, root, path), root, path, depth + 1)
    if "type" in schema and not _is_type(value, schema["type"]):
        _fail(path, "expected " + schema["type"])
    if "enum" in schema and not any(type(value) is type(option) and value == option for option in schema["enum"]):
        _fail(path, "value is not one of the allowed options")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            _fail(path, "text is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _fail(path, f"text is longer than {schema['maxLength']} characters")
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            _fail(path, "number is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            _fail(path, "number is above the maximum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            _fail(path, "too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            _fail(path, "too many items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate(item, schema["items"], root, f"{path}[{index}]", depth + 1)
    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            _fail(path, "too few properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            _fail(path, "too many properties")
        for name in schema.get("required", []):
            if name not in value:
                _fail(f"{path}.{name}", "required property is missing")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                validate(item, properties[name], root, f"{path}.{name}", depth + 1)
            elif extra is False:
                _fail(path, "unexpected property")
            elif type(extra) is not bool:
                validate(item, extra, root, path + ".*", depth + 1)
