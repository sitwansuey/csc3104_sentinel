"""Shared service identity credentials for Coordinator HTTP boundaries."""
import hmac
import json
import os
from pathlib import Path
import re

IDENTITY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class ProtocolError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def load_identities(path, allowed_roles):
    """Read role/token references; secret values come only from the environment."""
    entries = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(entries, dict) or not entries:
        raise ValueError("Expected a nonempty identity mapping")
    configured = {}
    for name, entry in entries.items():
        if (not isinstance(name, str) or not IDENTITY_NAME.fullmatch(name)
                or not isinstance(entry, dict) or set(entry) != {"role", "token_env"}
                or not isinstance(entry["role"], str) or entry["role"] not in allowed_roles
                or not isinstance(entry["token_env"], str) or not entry["token_env"]):
            raise ValueError("Invalid identity configuration")
        token = os.environ.get(entry["token_env"], "")
        if (len(token) < 32 or not token.isascii() or any(not 33 <= ord(char) <= 126 for char in token)
                or any(token == secret for _, secret in configured.values())):
            raise ValueError("Weak, missing or reused service credential")
        configured[name] = (entry["role"], token)
    return configured


def authenticate(headers, configured):
    """Authenticate exactly one identity and bearer header; return (name, role)."""
    names = headers.get_all("X-Service-Identity", [])
    credentials = headers.get_all("Authorization", [])
    if len(names) != 1 or len(credentials) != 1:
        raise ProtocolError(401, "Invalid credentials")
    name, auth = names[0], credentials[0]
    if name not in configured or not hmac.compare_digest(
            auth.encode("utf-8"), ("Bearer " + configured[name][1]).encode("utf-8")):
        raise ProtocolError(401, "Invalid credentials")
    return name, configured[name][0]
