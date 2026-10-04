"""V0-ORG-03: merge queue and rulesets as code (settings/<backend>.toml), applied by an admin.

`build` turns settings plus infra-config into one desired ruleset list per repo; product repos get
their required checks from V0-GAT-01 (`required.compute`). `readiness` says whether applying a repo's
rulesets is safe today: every required check must be a job in that repo's default branch that runs
on both pull_request and merge_group, or the queue would wait forever on a check that never reports.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from qqgate import backends, required
from qqgate.errors import GateError

SETTINGS = Path(__file__).resolve().parents[2] / "settings"
GATE_EVENTS = ("pull_request", "merge_group")


@dataclass(frozen=True)
class RepoPlan:
    name: str
    kind: str                 # product | infra
    checks: tuple[str, ...]   # every required check, gate-computed first
    rulesets: tuple[dict, ...]


def load_settings(backend: str, path: Path | None = None) -> dict:
    path = path or SETTINGS / f"{backend}.toml"
    if not path.is_file():
        raise GateError(f"no settings for backend {backend!r} ({path})")
    with path.open("rb") as f:
        return tomllib.load(f)


def _repos_in_config(cfg: dict) -> tuple[set[str], set[str]]:
    infra = {r["name"] for r in cfg["org"]["infra_repo"]}
    product = {r["name"] for r in cfg["repos"]["repo"]}
    return infra, product


def build(settings: dict, cfg: dict, config_root: Path) -> list[RepoPlan]:
    backend = cfg["gate"]["merge_queue"]["backend"]
    mod = backends.load(backend)
    infra, product = _repos_in_config(cfg)
    listed = [r["name"] for r in settings["repo"]]
    dupes = sorted({n for n in listed if listed.count(n) > 1})
    if dupes:
        raise GateError(f"settings list these repos twice: {dupes}")
    for name, want in (("infra", infra), ("product", product)):
        got = {r["name"] for r in settings["repo"] if r["kind"] == name}
        if got != want:
            raise GateError(f"settings {name} repos {sorted(got)} differ from infra-config's {sorted(want)}: "
                            f"missing {sorted(want - got)}, unknown {sorted(got - want)}")
    plans = []
    for r in settings["repo"]:
        if r["kind"] == "product":
            req = required.compute(cfg, r["name"])
            problems = mod.check_workflows(config_root, req)
            if problems:
                raise GateError(f"{r['name']}: " + "; ".join(problems))
            checks = tuple(req.names) + tuple(r.get("transitional_checks", []))
        else:
            checks = tuple(r.get("checks", []))
        if len(set(checks)) != len(checks):
            raise GateError(f"{r['name']}: a required check is listed twice: {checks}")
        rulesets = mod.rulesets(settings, cfg, checks)
        plans.append(RepoPlan(r["name"], r["kind"], checks, tuple(rulesets)))
    return plans


def workflow_jobs(checkout: Path) -> dict[str, set[str]]:
    """Job id (or `name:`) -> the events its workflow runs on, for every workflow in a checkout."""
    jobs: dict[str, set[str]] = {}
    for wf in sorted((checkout / ".github" / "workflows").glob("*.y*ml")):
        doc = yaml.safe_load(wf.read_text()) or {}
        on = doc.get("on", doc.get(True)) or {}
        events = {on} if isinstance(on, str) else set(on)
        for job_id, job in (doc.get("jobs") or {}).items():
            name = job.get("name", job_id) if isinstance(job, dict) else job_id
            jobs.setdefault(str(name), set()).update(events)
    return jobs


def readiness(plan: RepoPlan, checkout: Path) -> list[str]:
    """Why applying this repo's rulesets now would block it; empty means safe to apply."""
    if not (checkout / ".git").exists() and not (checkout / ".github").exists():
        return [f"no checkout at {checkout}"]
    jobs = workflow_jobs(checkout)
    out = []
    for c in plan.checks:
        if c not in jobs:
            out.append(f"required check {c!r} is not a job on the default branch (merge the PR that adds it first)")
            continue
        missing = [e for e in GATE_EVENTS if e not in jobs[c]]
        if missing:
            out.append(f"required check {c!r} does not run on {', '.join(missing)}; the queue would wait forever")
    return out
