"""Versioned scoring configuration and quirk switches.

A ``FormulaVersion`` bundles every constant used by the scores (``FormulaConfig``) with the
parity switches (``Quirks``). It round-trips to plain JSON-safe data so Step 5 can store it in
``formula_version.config`` and ``formula_version.quirks``. Loading is strict: an unknown key or a
value of the wrong type fails with a message naming the key.

Published versions are immutable. ``FormulaRegistry`` models the rule in memory; the database
enforces the same rule from Step 5 on.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

from scoring_engine.cash_health import CashHealthConfig
from scoring_engine.composite import CompositeConfig
from scoring_engine.effective_runway import RunwayAdjustConfig
from scoring_engine.milestone import MilestoneConfig
from scoring_engine.topology import TopologyConfig


class ConfigError(ValueError):
    """A formula configuration is invalid; the message names the offending key."""


class FormulaImmutableError(RuntimeError):
    """An attempt to change a formula version that has been used by a score run."""


@dataclass(frozen=True)
class Quirks:
    """Source quirks copied for parity. Each can be switched off individually.

    Only ``earnings_past_is_imminent`` and ``runway_as_filed`` change scores, so they are
    applied in this package. The other three are applied where the behaviour lives: the runway
    calculator (``ocf_ytd_naive_sum``, ``trial_burn_on_profitable``) and the effective-input
    resolver (``carry_forward``).
    """

    earnings_past_is_imminent: bool = True
    runway_as_filed: bool = True
    ocf_ytd_naive_sum: bool = True
    trial_burn_on_profitable: bool = True
    carry_forward: bool = False  # off for parity, on in production


@dataclass(frozen=True)
class FormulaConfig:
    topology: TopologyConfig = field(default_factory=TopologyConfig)
    cash_health: CashHealthConfig = field(default_factory=CashHealthConfig)
    milestone: MilestoneConfig = field(default_factory=MilestoneConfig)
    composite: CompositeConfig = field(default_factory=CompositeConfig)
    runway: RunwayAdjustConfig = field(default_factory=RunwayAdjustConfig)


@dataclass(frozen=True)
class FormulaVersion:
    name: str
    version: int
    config: FormulaConfig = field(default_factory=FormulaConfig)
    quirks: Quirks = field(default_factory=Quirks)
    notes: str = ""

    def to_plain(self) -> dict[str, Any]:
        """JSON-safe form: ``config`` and ``quirks`` match the formula_version columns."""
        return {
            "name": self.name,
            "version": self.version,
            "config": to_plain(self.config),
            "quirks": to_plain(self.quirks),
            "notes": self.notes,
        }

    @classmethod
    def from_plain(cls, data: dict[str, Any]) -> FormulaVersion:
        _reject_unknown(data, {"name", "version", "config", "quirks", "notes"}, "")
        name = data.get("name")
        version = data.get("version")
        if not isinstance(name, str) or not name:
            raise ConfigError("name: expected a non-empty string")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ConfigError("version: expected an integer")
        notes = data.get("notes", "")
        if not isinstance(notes, str):
            raise ConfigError("notes: expected a string")
        return cls(
            name=name,
            version=version,
            config=from_plain(FormulaConfig, data.get("config", {}), "config"),
            quirks=from_plain(Quirks, data.get("quirks", {}), "quirks"),
            notes=notes,
        )

    def content_hash(self) -> str:
        """Hash of everything that affects scores (config and quirks, not name or notes)."""
        body = {"config": to_plain(self.config), "quirks": to_plain(self.quirks)}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def with_quirks(self, **changes: bool) -> FormulaVersion:
        """A copy with some switches changed (the caller supplies a new name or version)."""
        unknown = set(changes) - {f.name for f in fields(Quirks)}
        if unknown:
            raise ConfigError(f"quirks.{sorted(unknown)[0]}: unknown switch")
        merged = {**to_plain(self.quirks), **changes}
        return FormulaVersion(
            self.name, self.version, self.config, from_plain(Quirks, merged, "quirks"), self.notes
        )


PARITY = FormulaVersion(
    name="parity",
    version=1,
    notes="Reproduces the existing system, quirks included. Carry-forward is off.",
)


# ------------------------------------------------------------------------ plain conversion


def to_plain(value: Any) -> Any:
    """Dataclasses become dicts and tuples become lists, recursively."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple | list):
        return [to_plain(item) for item in value]
    return value


def from_plain(cls: type[Any], data: Any, path: str) -> Any:
    """Build ``cls`` from plain data. Missing keys keep their defaults; unknown keys fail."""
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected an object")
    template = cls()
    _reject_unknown(data, {f.name for f in fields(cls)}, path)
    values: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        default = getattr(template, f.name)
        key_path = f"{path}.{f.name}"
        if is_dataclass(default):
            values[f.name] = from_plain(type(default), data[f.name], key_path)
        else:
            values[f.name] = _coerce(data[f.name], default, key_path)
    return cls(**values)


def _reject_unknown(data: dict[str, Any], allowed: set[str], path: str) -> None:
    for key in data:
        if key not in allowed:
            where = f"{path}.{key}" if path else key
            raise ConfigError(f"{where}: unknown key")


def _coerce(value: Any, default: Any, path: str) -> Any:
    """Check ``value`` has the shape and type of ``default`` and return it in that shape."""
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{path}: expected true or false")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: expected an integer")
        return value
    if isinstance(default, float):
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise ConfigError(f"{path}: expected a number")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{path}: expected a string")
        return value
    if isinstance(default, tuple):
        if not isinstance(value, list | tuple):
            raise ConfigError(f"{path}: expected a list")
        return _coerce_sequence(value, default, path)
    raise ConfigError(f"{path}: unsupported configuration type")  # pragma: no cover


def _coerce_sequence(
    value: list[Any] | tuple[Any, ...], default: tuple[Any, ...], path: str
) -> Any:
    sample = default[0] if default else None
    if isinstance(sample, tuple):
        rows = []
        for index, row in enumerate(value):
            row_path = f"{path}[{index}]"
            if not isinstance(row, list | tuple) or len(row) != len(sample):
                raise ConfigError(f"{row_path}: expected a list of {len(sample)} values")
            rows.append(
                tuple(
                    _coerce(cell, template, f"{row_path}[{i}]")
                    for i, (cell, template) in enumerate(zip(row, sample, strict=True))
                )
            )
        return tuple(rows)
    return tuple(_coerce(item, sample, f"{path}[{i}]") for i, item in enumerate(value))


# ----------------------------------------------------------------------------- immutability


class FormulaRegistry:
    """In-memory model of the formula_version table and its immutability rule."""

    def __init__(self) -> None:
        self._versions: dict[tuple[str, int], FormulaVersion] = {}
        self._used: set[tuple[str, int]] = set()

    def publish(self, formula: FormulaVersion) -> None:
        """Add a version. Publishing identical content again is harmless; different is not."""
        key = (formula.name, formula.version)
        existing = self._versions.get(key)
        if existing is not None and existing.content_hash() != formula.content_hash():
            raise FormulaImmutableError(
                f"{formula.name} v{formula.version} is already published; create a new version"
            )
        self._versions[key] = formula

    def get(self, name: str, version: int) -> FormulaVersion:
        return self._versions[(name, version)]

    def mark_used(self, name: str, version: int) -> None:
        """Record that a score run used this version, which freezes it."""
        if (name, version) not in self._versions:
            raise KeyError(f"{name} v{version} is not published")
        self._used.add((name, version))

    def edit(self, formula: FormulaVersion) -> None:
        """Replace a version's content; rejected once a score run has used it."""
        key = (formula.name, formula.version)
        if key in self._used:
            raise FormulaImmutableError(
                f"{formula.name} v{formula.version} was used by a score run; "
                "create a new version instead"
            )
        if key not in self._versions:
            raise KeyError(f"{formula.name} v{formula.version} is not published")
        self._versions[key] = formula
