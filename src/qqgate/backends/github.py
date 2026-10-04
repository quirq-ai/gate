"""GitHub backend: required checks are GitHub Actions jobs; the rule is a repository ruleset.

A generated workflow names its job after the builder ("The job name is the required check"), and
with no job `name:` GitHub reports the check run under the job id. So the required check context is
the builder name, and it must come from the GitHub Actions app, so another app cannot post a fake
green check with the same name.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from qqgate.errors import GateError
from qqgate.required import RequiredSet

GITHUB_ACTIONS_APP_ID = 15368  # the GitHub Actions app; checks from any other app do not count
API = "https://api.github.com"
EVENTS = {"change": "pull_request", "queue": "merge_group"}


def generated_workflow(config_root: Path, repo: str, builder: str) -> Path:
    return Path(config_root) / "generated" / "github" / repo / f"qq-{builder}.yml"


def check_workflows(config_root: Path, required: RequiredSet) -> list[str]:
    """Every required check must be a job in the workflow infra-config generates for it, triggered
    on pull_request and merge_group. A required check no workflow produces would block every PR."""
    problems = []
    for c in required.checks:
        path = generated_workflow(config_root, required.repo, c.builder)
        if not path.is_file():
            problems.append(f"{c.name}: infra-config generates no workflow for it ({path.name} missing); "
                            "set generate = true on the builder")
            continue
        doc = yaml.safe_load(path.read_text())
        on = doc.get("on", doc.get(True)) or {}  # YAML 1.1 reads a bare `on` key as True
        for trig in c.triggers:
            event = EVENTS.get(trig)
            if event and event not in on:
                problems.append(f"{c.name}: {path.name} does not run on {event}")
        jobs = doc.get("jobs") or {}
        job = jobs.get(c.name)
        if job is None:
            problems.append(f"{c.name}: {path.name} has no job {c.name!r}, so no check by that name reports")
        elif "name" in job and job["name"] != c.name:
            problems.append(f"{c.name}: job {c.name!r} in {path.name} reports as {job['name']!r}")
    return problems


def required_checks_rule(required: RequiredSet) -> dict:
    """The `required_status_checks` rule of a repository ruleset (REST: POST /repos/{o}/{r}/rulesets)."""
    return {
        "type": "required_status_checks",
        "parameters": {
            # The merge queue tests the exact merge result, so the branch need not be up to date.
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": False,
            "required_status_checks": [
                {"context": name, "integration_id": GITHUB_ACTIONS_APP_ID} for name in required.names
            ],
        },
    }


def owner_repo(source: str) -> str:
    """'github.com/quirq-ai/xo-space' -> 'quirq-ai/xo-space'."""
    parts = source.removeprefix("https://").strip("/").split("/")
    if len(parts) != 3 or parts[0] != "github.com":
        raise GateError(f"{source!r} is not a github.com/<owner>/<repo> source")
    return f"{parts[1]}/{parts[2]}"


def _get(url: str, token: str | None) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise GateError(f"GitHub API {e.code} for {url}: {e.reason}") from None


def observe(source: str, sha: str, token: str | None = None) -> dict[str, str]:
    """Check name -> conclusion (or status while running) for one commit, GitHub Actions checks only.
    If a check ran more than once, the newest run decides, as on GitHub."""
    token = token if token is not None else (os.environ.get("QQ_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    base = f"{API}/repos/{owner_repo(source)}/commits/{urllib.parse.quote(sha)}/check-runs"
    seen: dict[str, tuple[str, str]] = {}
    page = 1
    while True:
        data = _get(f"{base}?per_page=100&page={page}&app_id={GITHUB_ACTIONS_APP_ID}", token)
        for run in data.get("check_runs", []):
            state = run.get("conclusion") or run.get("status") or "unknown"
            started = run.get("started_at") or ""
            if run["name"] not in seen or started > seen[run["name"]][0]:
                seen[run["name"]] = (started, state)
        if page * 100 >= data.get("total_count", 0):
            break
        page += 1
    return {name: state for name, (_, state) in seen.items()}
