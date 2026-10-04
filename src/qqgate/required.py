"""V0-GAT-01: compute a repo's required checks from infra-config and its manifest.

Rules, all from config:
- gate.toml [merge_queue] required = "blocking-builders": every builder with blocking = true for the
  repo is a required check, named after the builder.
- Every required check runs on the proposed change and on the exact merge result: its triggers
  include both "change" and "queue". A blocking builder without "queue" is a config error, never a
  check that is silently skipped in the merge queue.
- A repo with no blocking builder is ungated, which is an error, not an empty list.
- With a manifest (read only through sync), every target's kind must be covered by a blocking
  builder, so no target can land unverified.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from qqgate.errors import GateError, NotOnboarded

GATE_TRIGGERS = ("change", "queue")  # the proposed change, and the exact merge result in the queue


@dataclass(frozen=True)
class Check:
    name: str            # the check's name on the backend; a builder's name
    builder: str
    kinds: tuple[str, ...]
    triggers: tuple[str, ...]
    backend: str


@dataclass(frozen=True)
class RequiredSet:
    repo: str
    backend: str
    merge_method: str
    checks: tuple[Check, ...]
    manifest_targets: tuple[str, ...] = field(default=())

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.checks]

    def to_json(self) -> dict:
        return {
            "repo": self.repo,
            "backend": self.backend,
            "merge_method": self.merge_method,
            "required": [
                {"name": c.name, "builder": c.builder, "kinds": list(c.kinds), "triggers": list(c.triggers)}
                for c in self.checks
            ],
            "manifest_targets": list(self.manifest_targets),
        }


def compute(cfg: dict, repo: str, manifest: dict | None = None) -> RequiredSet:
    gate = cfg["gate"]["merge_queue"]
    if gate["required"] != "blocking-builders":
        raise GateError(f"gate.toml [merge_queue] required = {gate['required']!r} is not a rule this gate knows")
    repos = {r["name"]: r for r in cfg["repos"]["repo"]}
    if repo not in repos:
        raise NotOnboarded(repo, sorted(repos))
    default_backend = cfg["pipelines"]["defaults"]["backend"]
    checks = []
    for b in cfg["pipelines"]["builder"]:
        if b["repo"] != repo or not b.get("blocking"):
            continue
        missing = [t for t in GATE_TRIGGERS if t not in b["triggers"]]
        if missing:
            raise GateError(f"builder {b['name']!r} is blocking but does not run on {missing}: every "
                            "required check must run on the change and on the exact merge result (queue)")
        backend = b.get("backend", default_backend)
        if backend != gate["backend"]:
            raise GateError(f"builder {b['name']!r} runs on backend {backend!r} but the merge queue "
                            f"is on {gate['backend']!r}; v0 gates on one backend")
        checks.append(Check(b["name"], b["name"], tuple(b["kinds"]), tuple(b["triggers"]), backend))
    if not checks:
        raise GateError(f"{repo!r} has no blocking builder in pipelines.toml, so nothing would gate it")
    targets: tuple[str, ...] = ()
    if manifest is not None:
        covered = {k for c in checks for k in c.kinds}
        uncovered = [f"{t['name']} ({t['kind']})" for t in manifest.get("targets", []) if t["kind"] not in covered]
        if uncovered:
            raise GateError(f"{repo}: manifest targets {', '.join(uncovered)} have no blocking builder "
                            f"for their kind; covered kinds are {sorted(covered)}")
        targets = tuple(t["name"] for t in manifest.get("targets", []))
    return RequiredSet(repo, gate["backend"], gate["merge_method"], tuple(checks), targets)


def load_manifest(path: Path, cfg: dict) -> dict:
    """Read infra/repo.toml through sync (qqsync), the only manifest parser."""
    try:
        from qqsync import manifest as qqsync_manifest
        from qqsync.errors import ManifestError
    except ImportError as e:  # pragma: no cover - install problem, not a gate decision
        raise GateError(f"reading a manifest needs qqsync (pins.toml [sync]): {e}") from None
    kinds = [k["name"] for k in cfg["kinds"]["kind"]]
    try:
        return qqsync_manifest.load(path, known_kinds=kinds)
    except ManifestError as e:
        raise GateError(f"manifest {path}: {e}") from None
