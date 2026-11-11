"""The one canonical JSON encoding and SHA-256 digest used across Sentinel.

Proposal, template, decision and transport digests bind Guard, Executor and
Coordinator together, so a one-byte change here breaks that binding. The output
is frozen by golden vectors in tests/test_canonical.py. Do not copy this
encoding into other modules; import it (tests/test_no_duplicate_canonical.py
enforces that).

Encoding: sorted keys, no spaces, ASCII-only output with escapes, and no NaN or
Infinity. Integers and floats stay distinct (1 and 1.0 encode differently).
Values that cannot be encoded (NaN, Infinity, sets, bytes, cycles, nesting too
deep) raise a CanonicalError. Each error also keeps its original built-in type
(ValueError, TypeError or RecursionError), so existing handlers still match.
"""

import hashlib
import json
from typing import Any


class CanonicalError(Exception):
    """A value has no canonical JSON encoding."""


class CanonicalValueError(CanonicalError, ValueError):
    """NaN, Infinity or a circular reference."""


class CanonicalTypeError(CanonicalError, TypeError):
    """A value or key of a type JSON cannot represent, such as a set or bytes."""


class CanonicalRecursionError(CanonicalError, RecursionError):
    """Nesting deeper than the interpreter recursion limit."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except RecursionError as exc:
        raise CanonicalRecursionError(*exc.args) from exc
    except TypeError as exc:
        raise CanonicalTypeError(*exc.args) from exc
    except ValueError as exc:
        raise CanonicalValueError(*exc.args) from exc


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
