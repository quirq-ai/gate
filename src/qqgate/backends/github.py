"""GitHub backend: required checks are GitHub Actions jobs; the rule is a repository ruleset.

A generated workflow names its job after the builder ("The job name is the required check"), and
with no job `name:` GitHub reports the check run under the job id. So the required check context is
the builder name, and it must come from the GitHub Actions app, so another app cannot post a fake
green check with the same name.

That does not stop a PR from adding its own workflow with a job of the same name: same app, same
name. So `observe` also requires each check to come from the workflow file infra-config generates
for it, and treats a same-named check from any other workflow as a spoof that fails the verdict.
TODO(expert): GitHub's ruleset rule has the same gap; V0-GAT-03 puts `.github/**` behind owner
review, which narrows it, and the merge queue runs workflows from the merge result.
"""
from __future__ import annotations

import json
import os
import urllib.error
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


def workflow_path(builder: str) -> str:
    """Where a delivered workflow lives in the product repo (qqcfg deliver)."""
    return f".github/workflows/qq-{builder}.yml"


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
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as e:
            raise GateError(f"{path}: not valid YAML: {e}") from None
        if not isinstance(doc, dict):
            raise GateError(f"{path}: not a workflow (top level is not a mapping)")
        on = doc.get("on", doc.get(True)) or {}
        if isinstance(on, (str, list)):
            on = {on} if isinstance(on, str) else set(on)  # YAML 1.1 reads a bare `on` key as True
        for trig in c.triggers:
            event = EVENTS.get(trig)
            if event and event not in on:
                problems.append(f"{c.name}: {path.name} does not run on {event}")
        jobs = doc.get("jobs") or {}
        job = jobs.get(c.name)
        if job is None:
            problems.append(f"{c.name}: {path.name} has no job {c.name!r}, so no check by that name reports")
        elif not isinstance(job, dict):
            problems.append(f"{c.name}: job {c.name!r} in {path.name} is not a mapping")
        else:
            if "name" in job and job["name"] != c.name:
                problems.append(f"{c.name}: job {c.name!r} in {path.name} reports as {job['name']!r}")
            # GitHub's rule treats a skipped job as passing; the gate never lets a required job skip.
            for key in ("if", "strategy"):
                if key in job:
                    problems.append(f"{c.name}: job {c.name!r} in {path.name} has `{key}:`, so it can skip "
                                    "or report under another name")
            if any(isinstance(v, dict) and ("paths" in v or "paths-ignore" in v) for v in on.values()
                   if isinstance(on, dict)):
                problems.append(f"{c.name}: {path.name} filters by paths, so the check may never report")
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
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GateError(f"GitHub API unreachable for {url}: {e}") from None
    except json.JSONDecodeError as e:
        raise GateError(f"GitHub API returned non-JSON for {url}: {e}") from None


SPOOFED = "spoofed"   # a same-named check from a workflow other than the generated one


def _pages(url: str, key: str, token: str | None) -> list[dict]:
    out, page = [], 1
    while True:
        data = _get(f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}", token)
        items = data.get(key, [])
        out += items
        if not items or page * 100 >= data.get("total_count", 0):
            return out
        page += 1


def observe(source: str, sha: str, expected: dict[str, str], token: str | None = None) -> dict[str, str]:
    """Check name -> conclusion (or status while running) for one commit.

    `expected` maps each required check to the workflow file that must produce it. Only GitHub
    Actions check runs from that workflow count. A same-named run from any other workflow makes the
    check SPOOFED, which is not green. If the right workflow ran more than once, the newest run
    decides; a run that has not started yet counts as newest, so a pending re-run fails closed.
    """
    token = token if token is not None else (os.environ.get("QQ_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    repo = owner_repo(source)
    q = urllib.parse.quote(sha)
    suites = {r["check_suite_id"]: r.get("path", "")
              for r in _pages(f"{API}/repos/{repo}/actions/runs?head_sha={q}", "workflow_runs", token)}
    runs = _pages(f"{API}/repos/{repo}/commits/{q}/check-runs?app_id={GITHUB_ACTIONS_APP_ID}&filter=all",
                  "check_runs", token)
    seen: dict[str, tuple[str, str]] = {}
    spoofed: set[str] = set()
    for run in runs:
        name = run["name"]
        if name not in expected:
            continue
        path = suites.get((run.get("check_suite") or {}).get("id"), "")
        # A workflow run's path may carry a ref suffix ("path@ref"); compare the file only.
        if path.split("@", 1)[0] != expected[name]:
            spoofed.add(name)
            continue
        state = run.get("conclusion") or run.get("status") or "unknown"
        started = run.get("started_at") or "9999"   # not started yet: newest, so it decides
        if name not in seen or started >= seen[name][0]:
            seen[name] = (started, state)
    out = {name: state for name, (_, state) in seen.items()}
    for name in spoofed:
        out[name] = SPOOFED
    return out
