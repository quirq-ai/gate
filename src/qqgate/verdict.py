"""The gate's verdict on one commit: every required check present and green, or refused.

Only "success" passes. A missing check is a refusal, not a pass: a builder that never reported
cannot have verified the change. GitHub's own required-check rule also accepts "neutral" and
"skipped"; the gate is stricter on purpose. TODO(expert): revisit once builders can skip by path.
"""
from __future__ import annotations

from dataclasses import dataclass

from qqgate.required import RequiredSet

PASSING = frozenset({"success"})


@dataclass(frozen=True)
class Verdict:
    repo: str
    passed: bool
    missing: tuple[str, ...]
    failing: tuple[tuple[str, str], ...]   # (check, conclusion or status)
    green: tuple[str, ...]

    def to_json(self) -> dict:
        return {
            "repo": self.repo,
            "verdict": "pass" if self.passed else "refused",
            "missing": list(self.missing),
            "failing": [{"name": n, "state": s} for n, s in self.failing],
            "green": list(self.green),
        }

    def lines(self) -> list[str]:
        out = [f"{self.repo}: {'PASS' if self.passed else 'REFUSED'}"]
        out += [f"  green    {n}" for n in self.green]
        out += [f"  failing  {n} ({s})" for n, s in self.failing]
        out += [f"  missing  {n} (never reported, so it verified nothing)" for n in self.missing]
        return out


def evaluate(required: RequiredSet, observed: dict[str, str]) -> Verdict:
    """`observed` maps a check name to its conclusion, or to its status while it is still running."""
    missing, failing, green = [], [], []
    for name in required.names:
        state = observed.get(name)
        if state is None:
            missing.append(name)
        elif state in PASSING:
            green.append(name)
        else:
            failing.append((name, state))
    return Verdict(required.repo, not missing and not failing, tuple(missing), tuple(failing), tuple(green))
