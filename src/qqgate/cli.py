"""qqgate: the quirq infra (qq) landing gate. Required checks come from infra-config and manifests.

    qqgate required --config DIR --repo NAME [--manifest PATH] [--json]
        The repo's required checks, after checking each one runs on the change and in the queue.
    qqgate rule --config DIR --repo NAME
        The backend's rule (GitHub: a ruleset rule) that makes those checks required.
    qqgate verdict --config DIR --repo NAME (--observed FILE | --sha SHA) [--json]
        Pass or refuse one commit. Exit 0 on pass, 1 when refused.
    qqgate settings plan|verify|apply --config DIR [...]   (V0-ORG-03)
        plan: every repo's rulesets as JSON. verify --checkouts DIR: which repos are safe to apply
        (each required check runs on pull_request and merge_group there). apply: create or update the
        rulesets of ready repos; dry run unless --yes. Needs an admin token in QQ_GITHUB_TOKEN.
    qqgate guard --repo NAME [ROOT]   (V0-GAT-02)
        Fail if a core repo's shipped code names a language, build tool or deploy target.
    qqgate queued-at [--event FILE] [--repository OWNER/NAME] [--json]   (V0-GAT-04)
        In a merge_group job: when the change entered the queue, as RFC 3339 UTC. Inside GitHub
        Actions it also exports QQ_QUEUED_AT to GITHUB_ENV for the result sink. Exit 1, nothing
        exported, when only an approximate time is available.
Exit codes: 0 ok or pass, 1 refused or not ready, 2 the gate could not decide.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import os

from qqgate import __version__, backends, config, guard, required, settings, verdict
from qqgate.errors import GateError


def _required(args) -> tuple[dict, required.RequiredSet]:
    cfg = config.load(Path(args.config), validate=not args.no_validate)
    manifest = required.load_manifest(Path(args.manifest), cfg) if args.manifest else None
    req = required.compute(cfg, args.repo, manifest)
    problems = backends.load(req.backend).check_workflows(Path(args.config), req)
    if problems:
        raise GateError(f"{args.repo}: required checks would not report:\n  " + "\n  ".join(problems))
    return cfg, req


def cmd_required(args) -> int:
    _, req = _required(args)
    if args.json:
        print(json.dumps(req.to_json(), indent=2))
    else:
        print(f"{req.repo} ({req.backend}, {req.merge_method} through the merge queue):")
        for c in req.checks:
            print(f"  {c.name:<32} kinds {', '.join(c.kinds)}; runs on {', '.join(c.triggers)}")
    return 0


def cmd_rule(args) -> int:
    _, req = _required(args)
    print(json.dumps(backends.load(req.backend).required_checks_rule(req), indent=2))
    return 0


def cmd_verdict(args) -> int:
    cfg, req = _required(args)
    if args.observed:
        try:
            observed = json.loads(Path(args.observed).read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise GateError(f"--observed {args.observed}: {e}") from None
        if not isinstance(observed, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                     for k, v in observed.items()):
            raise GateError(f"--observed {args.observed}: want a JSON object of check name -> conclusion")
    else:
        mod = backends.load(req.backend)
        source = next(r["source"] for r in cfg["repos"]["repo"] if r["name"] == req.repo)
        observed = mod.observe(source, args.sha, {c.name: mod.workflow_path(c.builder) for c in req.checks})
    v = verdict.evaluate(req, observed)
    print(json.dumps(v.to_json(), indent=2) if args.json else "\n".join(v.lines()))
    return 0 if v.passed else 1


def _plans(args):
    cfg = config.load(Path(args.config), validate=not args.no_validate)
    backend = cfg["gate"]["merge_queue"]["backend"]
    s = settings.load_settings(backend)
    plans = settings.build(s, cfg, Path(args.config))
    if args.repo:
        plans = [p for p in plans if p.name in args.repo]
        unknown = set(args.repo) - {p.name for p in plans}
        if unknown:
            raise GateError(f"not in settings: {sorted(unknown)}")
    return cfg, backend, s, plans


def _ready(plans, checkouts, s):
    allow = {r["name"]: tuple(r.get("allow_conditional", ())) for r in s["repo"]}
    return {p.name: settings.readiness(p, Path(checkouts) / p.name, allow.get(p.name, ())) for p in plans}


def cmd_settings(args) -> int:
    cfg, backend, s, plans = _plans(args)
    mod = backends.load(backend)
    if args.action == "plan":
        out = {p.name: {"kind": p.kind, "required": list(p.checks), "rulesets": list(p.rulesets)} for p in plans}
        if not args.repo:
            out["(org)"] = {"rulesets": [mod.org_ruleset(s, cfg, repository_id=0)]}  # id looked up at apply
        print(json.dumps(out, indent=2))
        return 0
    if not args.checkouts:
        raise GateError(f"settings {args.action} needs --checkouts DIR with each repo's default branch")
    ready = _ready(plans, args.checkouts, s)
    for p in plans:
        why = ready[p.name]
        print(f"{'ready    ' if not why else 'NOT READY'} {p.name:<15} required: {', '.join(p.checks) or '(none yet)'}")
        for w in why:
            print(f"            {w}")
    if args.action == "verify":
        return 0 if all(not w for w in ready.values()) else 1
    token = os.environ.get("QQ_GITHUB_TOKEN")
    if not token:
        raise GateError("apply needs an admin token in QQ_GITHUB_TOKEN (for example: QQ_GITHUB_TOKEN=$(gh auth token))")
    for p in plans:
        if ready[p.name]:
            print(f"skip     {p.name}: not ready")
            continue
        for line in mod.existing_protection(s["org"]["owner"], p.name,
                                            {rs["name"] for rs in p.rulesets} | {s["org_workflows"]["ruleset"]}, token):
            print(f"WARNING  {p.name}: {line}")
        for line in mod.apply(s["org"]["owner"], p.name, list(p.rulesets), token, write=args.yes):
            print(line)
    # Last, and on its own: an org ruleset needs admin:org and a plan that offers required workflows.
    if args.org and not s["org_workflows"].get("enabled", False):
        print("qqgate: org ruleset not applied: settings [org_workflows] enabled = false", file=sys.stderr)
        return 2
    if args.org:
        try:
            for line in mod.apply_org(s["org"]["owner"], s, cfg, token, write=args.yes):
                print(line)
        except GateError as e:
            print(f"qqgate: org ruleset not applied: {e}", file=sys.stderr)
            return 2
    return 0 if all(not w for w in ready.values()) else 1


def cmd_guard(args) -> int:
    findings = guard.scan_repo(Path(args.root), args.repo, guard.load_terms())
    for f in findings:
        print(f"::error file={f.path},line={f.line}::{f}" if args.github else str(f))
    print(f"{args.repo}: {len(findings)} finding(s); core code must stay agnostic (plan §5.1)"
          if findings else f"{args.repo}: agnostic")
    return 1 if findings else 0


def cmd_queued_at(args) -> int:
    event_path = args.event or os.environ.get("GITHUB_EVENT_PATH")
    repository = args.repository or os.environ.get("GITHUB_REPOSITORY")
    if not event_path or not repository:
        raise GateError("queued-at needs --event and --repository (or GITHUB_EVENT_PATH and GITHUB_REPOSITORY)")
    try:
        event = json.loads(Path(event_path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise GateError(f"event {event_path}: {e}") from None
    q = backends.load(args.backend).queued_at(event, repository)
    if not q.exact:  # an approximate time would skew p50/p90 unseen, so the sink gets none
        print(f"qqgate: only an approximate queue time ({q.at}, {q.source}); not exported", file=sys.stderr)
        return 1
    print(json.dumps({"queued_at": q.at, "source": q.source, "exact": q.exact}) if args.json else q.at)
    env_file = os.environ.get("GITHUB_ENV")
    if env_file and not args.no_export:
        with open(env_file, "a") as f:
            f.write(f"QQ_QUEUED_AT={q.at}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qqgate", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--version", action="version", version=f"qqgate {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (("required", cmd_required, "list a repo's required checks"),
                            ("rule", cmd_rule, "print the backend rule that requires them"),
                            ("verdict", cmd_verdict, "pass or refuse one commit")):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        p.add_argument("--config", required=True, help="infra-config checkout (pins.toml [infra-config])")
        p.add_argument("--repo", required=True, help="product repo name from repos.toml")
        p.add_argument("--manifest", help="the repo's infra/repo.toml, read through qqsync")
        p.add_argument("--no-validate", action="store_true", help=argparse.SUPPRESS)  # tests only
        if name in ("required", "verdict"):
            p.add_argument("--json", action="store_true")
        if name == "verdict":
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--observed", help="JSON file: check name -> conclusion")
            g.add_argument("--sha", help="commit to read check results for from the backend")
    st = sub.add_parser("settings", help="merge queue and rulesets as code (V0-ORG-03)")
    st.set_defaults(fn=cmd_settings)
    st.add_argument("action", choices=["plan", "verify", "apply"])
    st.add_argument("--config", required=True, help="infra-config checkout (pins.toml [infra-config])")
    st.add_argument("--repo", action="append", help="limit to these repos (repeatable)")
    st.add_argument("--checkouts", help="directory holding a default-branch checkout of each repo, by name")
    st.add_argument("--yes", action="store_true", help="apply: really write (default is a dry run)")
    st.add_argument("--org", action="store_true", help="apply: also the org ruleset (qq-drift workflow), once settings enable it")
    st.add_argument("--no-validate", action="store_true", help="skip qqcfg validate (tests only)")
    gd = sub.add_parser("guard", help="agnosticism guard for a core repo (V0-GAT-02)")
    gd.set_defaults(fn=cmd_guard)
    gd.add_argument("--repo", required=True, help="core repo name (guard/terms.toml core)")
    gd.add_argument("--github", action="store_true", help="print GitHub annotations")
    gd.add_argument("root", nargs="?", default=".", help="the repo checkout (default: .)")
    qa = sub.add_parser("queued-at", help="when this gate run's change entered the queue (V0-GAT-04)")
    qa.set_defaults(fn=cmd_queued_at)
    qa.add_argument("--event", help="the event payload JSON (default: GITHUB_EVENT_PATH)")
    qa.add_argument("--repository", help="owner/name (default: GITHUB_REPOSITORY)")
    qa.add_argument("--backend", default="github")
    qa.add_argument("--json", action="store_true")
    qa.add_argument("--no-export", action="store_true", help="do not write QQ_QUEUED_AT to GITHUB_ENV")
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except GateError as e:
        print(f"qqgate: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # a crash is "could not decide" (2), never "refused" (1) or "pass" (0)
        print(f"qqgate: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
