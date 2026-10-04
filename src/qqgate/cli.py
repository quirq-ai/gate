"""qqgate: the quirq infra (qq) landing gate. Required checks come from infra-config and manifests.

    qqgate required --config DIR --repo NAME [--manifest PATH] [--json]
        The repo's required checks, after checking each one runs on the change and in the queue.
    qqgate rule --config DIR --repo NAME
        The backend's rule (GitHub: a ruleset rule) that makes those checks required.
    qqgate verdict --config DIR --repo NAME (--observed FILE | --sha SHA) [--json]
        Pass or refuse one commit. Exit 0 on pass, 1 when refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qqgate import __version__, backends, config, required, verdict
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
        observed = json.loads(Path(args.observed).read_text())
    else:
        source = next(r["source"] for r in cfg["repos"]["repo"] if r["name"] == req.repo)
        observed = backends.load(req.backend).observe(source, args.sha)
    v = verdict.evaluate(req, observed)
    print(json.dumps(v.to_json(), indent=2) if args.json else "\n".join(v.lines()))
    return 0 if v.passed else 1


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
        p.add_argument("--no-validate", action="store_true", help="skip qqcfg validate (tests only)")
        if name in ("required", "verdict"):
            p.add_argument("--json", action="store_true")
        if name == "verdict":
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--observed", help="JSON file: check name -> conclusion")
            g.add_argument("--sha", help="commit to read check results for from the backend")
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except GateError as e:
        print(f"qqgate: {e}", file=sys.stderr)
        return 2
