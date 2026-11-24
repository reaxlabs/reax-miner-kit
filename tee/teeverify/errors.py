"""Result types shared by all verifiers. Everything fails closed: a check that cannot run is a failed check."""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any, Callable


class EvidenceError(Exception):
    """Evidence is malformed, unsigned, or fails a cryptographic check."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    """Collects named checks. `ok` is True only if at least one check ran and all passed."""

    kind: str
    checks: list[Check] = field(default_factory=list)
    claims: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "checks": [c.__dict__ for c in self.checks],
            "claims": self.claims,
        }


def fail_closed(kind: str) -> Callable:
    """Decorator: any unexpected exception from crafted input becomes a failed check, never a crash or a pass."""

    def deco(fn: Callable[..., Report]) -> Callable[..., Report]:
        @functools.wraps(fn)
        def wrapper(*a: Any, **kw: Any) -> Report:
            try:
                return fn(*a, **kw)
            except Exception as e:  # noqa: BLE001 - deliberate: untrusted input
                r = Report(kind)
                r.add("internal_error", False, f"{type(e).__name__} on untrusted input")
                return r

        return wrapper

    return deco
