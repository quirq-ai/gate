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


@dataclass
class Job:
    events: set[str]
    workflows: list[str]
    condition: str = ""        # job-level `if:`; a skipped job counts as passing on GitHub
    needs: tuple[str, ...] = ()
    matrix: bool = False       # matrix legs report as "name (a, b)", never as the bare name
    path_filtered: bool = False


def _events(on) -> tuple[set[str], bool]:
    if isinstance(on, str):
        return {on}, False
    if isinstance(on, list):
        return set(on), False
    on = on or {}
    filtered = any(isinstance(v, dict) and ("paths" in v or "paths-ignore" in v)
                   for k, v in on.items() if k in GATE_EVENTS)
    return set(on), filtered


def workflow_jobs(checkout: Path) -> dict[str, Job]:
    """Check name (job `name:` or id) -> how it runs, across every workflow in a checkout."""
    jobs: dict[str, Job] = {}
    for wf in sorted((checkout / ".github" / "workflows").glob("*.y*ml")):
        try:
            doc = yaml.safe_load(wf.read_text()) or {}
        except yaml.YAMLError as e:
            raise GateError(f"{wf}: not valid YAML: {e}") from None
        if not isinstance(doc, dict):
            continue
        events, filtered = _events(doc.get("on", doc.get(True)))
        for job_id, job in (doc.get("jobs") or {}).items():
            job = job if isinstance(job, dict) else {}
            name = str(job.get("name", job_id))
            needs = job.get("needs", ())
            j = jobs.setdefault(name, Job(set(), []))
            j.events |= events
            j.workflows.append(wf.name)
            j.condition = str(job.get("if", "")) or j.condition
            j.needs = tuple([needs] if isinstance(needs, str) else needs) or j.needs
            j.matrix = j.matrix or "matrix" in (job.get("strategy") or {})
            j.path_filtered = j.path_filtered or filtered
    return jobs


def readiness(plan: RepoPlan, checkout: Path, allow_conditional: tuple[str, ...] = ()) -> list[str]:
    """Why applying this repo's rulesets now would block it or let red through; empty means safe.

    GitHub counts a skipped required check as passing, so a required job must not skip: no `if:`
    (unless `if: always()`-style or listed in the repo's `allow_conditional`), and no `needs:` without
    `always()`, because a failed dependency skips it. A matrix job or a path-filtered workflow may
    never report under the required name, which would wedge the queue.
    """
    if not (checkout / ".git").exists() and not (checkout / ".github").exists():
        return [f"no checkout at {checkout}"]
    jobs = workflow_jobs(checkout)
    out = []
    for c in plan.checks:
        j = jobs.get(c)
        if j is None:
            out.append(f"required check {c!r} is not a job on the default branch (merge the PR that adds it first)")
            continue
        if len(j.workflows) > 1:
            out.append(f"required check {c!r} is a job in several workflows ({', '.join(j.workflows)}); "
                       "rename one so the name means one thing")
        missing = [e for e in GATE_EVENTS if e not in j.events]
        if missing:
            out.append(f"required check {c!r} does not run on {', '.join(missing)}; the queue would wait forever")
        if j.matrix:
            out.append(f"required check {c!r} is a matrix job; its legs report under other names")
        if j.path_filtered:
            out.append(f"required check {c!r} is in a workflow filtered by paths, so it may never report")
        always = "always()" in j.condition
        if j.needs and not always:
            out.append(f"required check {c!r} needs {', '.join(j.needs)} without `if: always()`: a failed "
                       "dependency skips it, and GitHub counts a skipped required check as passing")
        elif j.condition and not always and c not in allow_conditional:
            out.append(f"required check {c!r} has `if: {j.condition}`; when it skips, GitHub counts it as "
                       "passing (list it in allow_conditional with a reason if that skip is intended)")
    return out
