"""Unified result envelope for Core and bridge responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CoreResult:
    ok: bool
    data: Any = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CoreResult:
        return cls(
            ok=bool(d.get("ok", False)),
            data=d.get("data"),
            warnings=list(d.get("warnings") or []),
            errors=list(d.get("errors") or []),
        )


def merge_results(primary: CoreResult, secondary: CoreResult) -> CoreResult:
    """Combine two results (e.g. batch step); ok is true only if both ok."""
    ok = primary.ok and secondary.ok
    warnings = [*primary.warnings, *secondary.warnings]
    errors = [*primary.errors, *secondary.errors]
    data = {"primary": primary.data, "secondary": secondary.data}
    return CoreResult(ok=ok, data=data, warnings=warnings, errors=errors)
