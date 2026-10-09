"""qqgate: the quirq infra (qq) landing gate. Required checks come from infra-config and manifests.

    qqgate required --config DIR --repo NAME [--manifest PATH] [--json]
        The repo's required checks, after checking each one runs on the change and in the queue.
    qqgate rule --config DIR --repo NAME
        The backend's rule (GitHub: a ruleset rule) that makes those checks required.
    qqgate verdict --config DIR --repo NAME (--observed FILE | --sha SHA) [--json]
        Pass or refuse one commit. Exit 0 on pass, 1 when refused.
    qqgate settings plan|verify|apply --config DIR [...]   (V0-ORG-03)
        plan: every repo's rulesets as JSON. verify --checkouts DIR: which repos are safe to apply
        (each checkout is its default branch's current head, and each required check runs on
        pull_request and merge_group there). apply: create or update the rulesets of ready repos; dry
        run unless --yes. Needs an admin token in QQ_GITHUB_TOKEN; the plan is computed in a child
        process without it. --org also applies the enabled org rulesets (admin:org).
        With --settings FILE --infra-config DIR (one-command setup): a user org's qq-main and
        qq-reserved-tags, from FILE, with --config a data-only qq-config checkout whose qqcfg code comes
        from --infra-config; verify and apply also need --config-commit SHA.
    qqgate settings check --settings FILE [--repo NAME]
        Read-only (GETs): what already protects each repo in FILE (classic protection, other rulesets,
        required reviews, squash off), its visibility and Actions permissions, whatever its
        readiness. Exit 0 no warnings, 1 warnings, 2 error. Loads no config and no infra-config code.
    qqgate guard --repo NAME [ROOT]   (V0-GAT-02)
        Fail if a core repo's shipped code names a language, build tool or deploy target.
    qqgate queued-at [--event FILE] [--repository OWNER/NAME] [--json]   (V0-GAT-04)
        In a merge_group job: when the change entered the queue, as RFC 3339 UTC. Inside GitHub
        Actions it also exports QQ_QUEUED_AT to GITHUB_ENV for the result sink. Exit 1, nothing
        exported, when only an approximate time is available.
Exit codes: 0 ok or pass, 1 refused or not ready, 2 the gate could not decide, 3 (required,
rule, verdict) the repo is not in repos.toml, so no gate applies (--json: {"onboarded": false}).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


from qqgate import __version__, backends, config, guard, required, settings, verdict
from qqgate.errors import GateError, NotOnboarded


def _required(args) -> tuple[dict, required.RequiredSet]:
    cfg = config.load(Path(args.config), validate=not args.no_validate)
    known = sorted(r["name"] for r in cfg["repos"]["repo"])
    if args.repo not in known:  # before the manifest, so an unknown repo is always exit 3
        raise NotOnboarded(args.repo, known)
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


def _user_mode(args) -> bool:
    """`--settings FILE`: a user org's settings (one-command setup), never gate's own file. The flags
    exist only on `settings` and have no environment variable; scripts/apply.sh never passes them."""
    if getattr(args, "settings", None) is None:
        if getattr(args, "infra_config", None) or getattr(args, "config_commit", None):
            raise GateError("--infra-config and --config-commit go with --settings")
        return False
    if getattr(args, "org", False):
        raise GateError("--org is not available with --settings: a user org gets no org rulesets")
    return True


def _settings_file(args) -> tuple[dict, Path]:
    """The settings this run uses, and the file they come from."""
    if _user_mode(args):
        path = Path(args.settings).resolve()
        return settings.load_user_settings(path), path
    return settings.load_settings("github"), settings.SETTINGS / "github.toml"


def _plans(args):
    user = _user_mode(args)
    cfg = config.load(Path(args.config), validate=not args.no_validate,
                      code=Path(args.infra_config) if user else None)
    backend = cfg["gate"]["merge_queue"]["backend"]
    if user:
        if backend != "github":
            raise GateError(f"config names backend {backend!r}; a user org's settings are for 'github'")
        s = settings.load_user_settings(Path(args.settings).resolve())
        settings.check_user_owner(s, cfg["org"]["org"].get("code_host"))
    else:
        s = settings.load_settings(backend)
    plans = settings.build(s, cfg, Path(args.config))
    if args.repo:
        plans = [p for p in plans if p.name in args.repo]
        unknown = set(args.repo) - {p.name for p in plans}
        if unknown:
            raise GateError(f"not in settings: {sorted(unknown)}")
    return cfg, backend, s, plans


def _plan_json(cfg: dict, backend: str, plans, org: list[dict]) -> dict:
    out = {"(backend)": backend,
           "(gate)": {"merge_method": cfg["gate"]["merge_queue"]["merge_method"],
                      "max_minutes": cfg["gate"]["admission"]["max_minutes"]}}
    out.update({p.name: {"kind": p.kind, "required": list(p.checks), "rulesets": list(p.rulesets)} for p in plans})
    out["(org)"] = org
    return out


# The child gets only what the interpreter needs, and an empty HOME: no tokens, no SSH agent, no
# netrc, no gh or git config. It still runs as the same OS user, so the pinned, reviewed infra-config
# commit is what makes its code trustworthy; this keeps credentials out of its reach by default.
_CHILD_ENV = ("PATH", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "VIRTUAL_ENV",
              "PYTHONIOENCODING", "PYTHONUTF8")


def _child_env(home: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k in _CHILD_ENV or k.startswith("LC_")}
    env.update(HOME=home, USERPROFILE=home, XDG_CONFIG_HOME=home, PYTHONNOUSERSITE="1")
    return env


def _plan_without_token(args) -> dict:
    """Run `settings plan` in a child process with no credentials in its environment (audit S8).
    Planning executes infra-config's tools/qqcfg.py; the process holding the admin token never does,
    and it rebuilds every ruleset itself from settings (`_trusted_plans`), so the child only supplies
    product repos' required check names and gate.toml's merge method and admission limit."""
    cmd = [sys.executable, "-c", "import sys; from qqgate.cli import main; sys.exit(main(sys.argv[1:]))",
           "settings", "plan", "--config", str(Path(args.config).resolve())]
    if _user_mode(args):  # the child checks and plans the same file the parent read (and re-reads after)
        cmd += ["--settings", str(Path(args.settings).resolve()), "--infra-config", str(Path(args.infra_config).resolve())]
    for r in args.repo or ():
        cmd += ["--repo", r]
    if args.no_validate:
        cmd.append("--no-validate")
    with tempfile.TemporaryDirectory(prefix="qqgate-plan-") as home:
        try:
            r = subprocess.run(cmd, env=_child_env(home), cwd=home, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GateError(f"computing the plan failed: {e}") from None
    if r.returncode != 0:
        raise GateError(f"computing the plan failed (exit {r.returncode}): {r.stderr.strip()}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise GateError(f"computing the plan printed something other than JSON: {e}") from None
    if not isinstance(data, dict):
        raise GateError("computing the plan printed JSON that is not an object")
    return data


CHECK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,99}$")


def _trusted_plans(data: dict, wanted: list[str] | None, s: dict, config_root: Path):
    """Rebuild the plan in this process from settings `s` (read before the child ran), taking only
    check names and two gate.toml values from the child, each validated; refuse if the child's
    rulesets differ. A product repo's gate-computed checks must each be a job in the workflow the
    pinned infra-config generates for it (read here as YAML, no infra-config code runs)."""
    backend = data.get("(backend)")
    if backend != "github":
        raise GateError(f"plan names backend {backend!r}; only 'github' has settings")
    mod = backends.load(backend)
    gate = data.get("(gate)") or {}
    method, minutes = gate.get("merge_method"), gate.get("max_minutes")
    if method != s["main"]["merge_method"] or not isinstance(minutes, int) or not 1 <= minutes <= 360:
        raise GateError(f"plan's gate values {gate!r} are not plausible (merge_method must be "
                        f"{s['main']['merge_method']!r}, max_minutes 1 to 360)")
    cfg = {"gate": {"merge_queue": {"merge_method": method}, "admission": {"max_minutes": minutes}},
           "repos": {"repo": [{"name": r["name"]} for r in s["repo"] if r["kind"] == "product"]}}
    names = [r["name"] for r in s["repo"] if not wanted or r["name"] in wanted]
    got = sorted(k for k in data if not k.startswith("("))
    if got != sorted(names):
        raise GateError(f"plan lists repos {got}, settings say {sorted(names)}")
    plans = []
    for r in (r for r in s["repo"] if r["name"] in names):
        d = data[r["name"]]
        required = d.get("required")
        if d.get("kind") != r["kind"] or not isinstance(required, list) or not all(
                isinstance(c, str) and CHECK_NAME.match(c) for c in required) or len(set(required)) != len(required):
            raise GateError(f"plan entry for {r['name']} is not plausible: {d.get('kind')!r} {required!r}")
        if r["kind"] == "infra" and required != list(r.get("checks", [])):
            raise GateError(f"plan's checks for {r['name']} {required} differ from settings {r.get('checks', [])}")
        tail = list(r.get("transitional_checks", []))
        if r["kind"] == "product" and (not required[len(required) - len(tail):] == tail or len(required) <= len(tail)):
            raise GateError(f"plan's checks for {r['name']} {required} do not end with transitional {tail}")
        if r["kind"] == "product":
            for c in required[:len(required) - len(tail)]:
                if c not in mod.generated_jobs(config_root, r["name"], c):
                    raise GateError(f"plan's check {c!r} for {r['name']} is not a job in a workflow the pinned "
                                    "infra-config generates")
        rulesets = mod.rulesets(s, cfg, tuple(required), **settings.repo_options(r, s))
        if rulesets != d.get("rulesets"):
            raise GateError(f"plan's rulesets for {r['name']} differ from what settings/github.toml builds")
        plans.append(settings.RepoPlan(r["name"], r["kind"], tuple(required), tuple(rulesets)))
    return s, mod, plans, settings.org_workflows(s, cfg)


def _config_at_pin(config_root: Path) -> str | None:
    """None when the infra-config checkout is at pins.toml's commit, else why not."""
    pins = Path(__file__).resolve().parents[2] / "pins.toml"
    want = tomllib.loads(pins.read_text())["infra-config"]["commit"]
    head = settings._git(config_root, "rev-parse", "HEAD")
    if head != want:
        return f"--config {config_root} is at {head or 'no commit'}, not pins.toml's infra-config {want}"
    if settings._git(config_root, "status", "--porcelain", "--untracked-files=no"):
        return f"--config {config_root} has local changes; use a clean checkout of {want}"
    return None


def _origin(checkout: Path) -> str:
    """origin's URL, lower case and without .git or a trailing slash (GitHub names ignore case)."""
    url = settings._git(checkout, "config", "--get", "remote.origin.url") or ""
    return url.rstrip("/").removesuffix(".git").lower()


def _infra_config_source() -> str:
    """quirq-ai/infra-config, as pins.toml names it."""
    return tomllib.loads((Path(__file__).resolve().parents[2] / "pins.toml").read_text())["infra-config"]["source"]


def _user_commits(args, s: dict) -> str | None:
    """None when --config is exactly qq-config's --config-commit (clean, from the settings owner's
    qq-config) and --infra-config is quirq-ai/infra-config at qq-config's qq.toml pin, a commit on its
    main; else why not. Replaces the pins.toml check of gate's own settings."""
    data, code, want = Path(args.config), Path(args.infra_config or "."), args.config_commit or ""
    owner = s["org"]["owner"]
    if not re.fullmatch(r"[0-9a-f]{40}", want):
        return f"--settings needs --config-commit, qq-config's full 40-hex commit, not {want or 'nothing'!r}"
    head = settings._git(data, "rev-parse", "HEAD")
    if head != want:
        return f"--config {data} is at {head or 'no commit'}, not --config-commit {want}"
    if settings._git(data, "status", "--porcelain"):
        return f"--config {data} has local changes or untracked files; use a clean checkout of {want}"
    if _origin(data) != f"https://github.com/{owner}/{settings.USER_CONFIG_REPO}".lower():
        return f"--config {data} was not cloned from https://github.com/{owner}/{settings.USER_CONFIG_REPO}"
    try:
        org = tomllib.loads((data / "config" / "org.toml").read_text())
        settings.check_user_owner(s, org["org"]["code_host"])
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as e:
        return f"--config {data}: cannot read config/org.toml's code_host: {e}"
    except GateError as e:
        return str(e)
    try:
        pin = tomllib.loads((data / "qq.toml").read_text())["infra-config"]["commit"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as e:
        return f"--config {data}: cannot read the infra-config pin from qq.toml ([infra-config] commit): {e}"
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{40}", pin):
        return f"--config {data}: qq.toml's infra-config commit {pin!r} is not a full 40-hex commit"
    source = _infra_config_source()
    if settings._git(code, "rev-parse", "HEAD") != pin:
        return f"--infra-config {code} is not at qq.toml's pin {pin}"
    if settings._git(code, "status", "--porcelain", "--untracked-files=no"):
        return f"--infra-config {code} has local changes; use a clean checkout of {pin}"
    if _origin(code) != source.rstrip("/").removesuffix(".git").lower():
        return f"--infra-config {code} was not cloned from {source}"
    main = (settings._git(code, "ls-remote", "origin", "refs/heads/main") or "").split("\t")[0]
    if not re.fullmatch(r"[0-9a-f]{40}", main):
        return f"--infra-config {code}: cannot read {source} main to confirm the pin is on it"
    if settings._git(code, "cat-file", "-e", f"{main}^{{commit}}") is None:
        settings._git(code, "fetch", "--quiet", "origin", "main")
    if settings._git(code, "merge-base", "--is-ancestor", pin, main) is None:
        return f"qq.toml's infra-config pin {pin} is not on {source} main"
    return None


def _ready(plans, checkouts, s):
    """Readiness per repo, from a checkout that must be the remote default branch's current head."""
    allow = {r["name"]: tuple(r.get("allow_conditional", ())) for r in s["repo"]}
    out, heads = {}, {}
    for p in plans:
        co = Path(checkouts) / p.name
        st = settings.checkout_state(co, f"https://github.com/{s['org']['owner']}/{p.name}")
        heads[p.name] = st.head
        found = settings.readiness(p, co, allow.get(p.name, ()), st.branch) if st.head else []
        out[p.name] = list(st.problems) + [w for w in found if w not in st.problems]
    return out, heads


def _print_ready(plans, ready, heads):
    for p in plans:
        why = ready[p.name]
        at = (heads[p.name] or "no commits")[:12]
        print(f"{'ready    ' if not why else 'NOT READY'} {p.name:<15} at {at:<12} "
              f"required: {', '.join(p.checks) or '(none)'}", flush=True)
        for w in why:
            print(f"            {w}", flush=True)


def cmd_settings(args) -> int:
    if args.action == "check":
        return _check(args)
    if not args.config:
        raise GateError(f"settings {args.action} needs --config DIR")
    if _user_mode(args) and not args.infra_config:
        raise GateError("--settings needs --infra-config DIR, the infra-config checkout at qq-config's qq.toml pin")
    if args.action == "plan":
        cfg, backend, s, plans = _plans(args)
        print(json.dumps(_plan_json(cfg, backend, plans, settings.org_workflows(s, cfg)), indent=2))
        return 0
    if not args.checkouts:
        raise GateError(f"settings {args.action} needs --checkouts DIR with each repo's default branch")
    if args.action == "verify":
        s, _ = _settings_file(args)
        stale = _user_commits(args, s) if _user_mode(args) else _config_at_pin(Path(args.config))
        if stale:
            print(f"WARNING  {stale}", flush=True)
        # Like apply, infra-config's code runs only in the credential-free child (review of #13:
        # verify runs in the admin's own environment, with gh's login in reach).
        data = _plan_without_token(args)
        s, _, plans, org = _trusted_plans(data, args.repo, s, Path(args.config))
        ready, heads = _ready(plans, args.checkouts, s)
        _print_ready(plans, ready, heads)
        org_bad = 0
        for w in org:
            if not w.get("enabled", False):
                continue
            why = settings.org_workflow_readiness(w, Path(args.checkouts), data["(gate)"]["max_minutes"],
                                                  s["org"]["owner"])
            org_bad += bool(why)
            print(f"{'ready    ' if not why else 'NOT READY'} org {w['ruleset']}", flush=True)
            for line in why:
                print(f"            {line}", flush=True)
        return 0 if all(not w for w in ready.values()) and not org_bad else 1
    return _apply(args)


def _token(child_ran: bool = True) -> str:
    """The admin token, read only after the plan child has exited: while infra-config code runs, no
    credential is in this process (a same-user child can read its parent's environment)."""
    token = os.environ.get("QQ_GITHUB_TOKEN")
    if token and child_ran:
        print("WARNING  QQ_GITHUB_TOKEN was in this process's environment while infra-config's code ran in "
              "the plan child, which can read it; leave it unset and qqgate asks `gh auth token` afterwards",
              flush=True)
        return token
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GateError(f"apply needs a token: `gh auth token` failed ({e}); run `gh auth login` first") from None
    if r.returncode != 0 or not r.stdout.strip():
        raise GateError(f"apply needs a token: `gh auth token` failed: {r.stderr.strip()}; run `gh auth login` first")
    return r.stdout.strip()


def _print_change(c: dict) -> None:
    if c.get("kind") == "setting":
        v = json.dumps(c["body"][c["name"]])
        now = "" if c["action"] == "unchanged" else f" (now {json.dumps(c['now'])})"
        print(f"plan     {c['where']}: {c['action']} setting {c['name']} = {v}{now}", flush=True)
        return
    extra = f" (differs: {', '.join(c['diff'])})" if c["diff"] else ""
    print(f"plan     {c['where']}: {c['action']} ruleset {c['name']}{extra}", flush=True)


def _apply(args) -> int:
    user = _user_mode(args)
    if not user:
        stale = _config_at_pin(Path(args.config))
        if stale:
            raise GateError(f"{stale}; check out the pinned commit (docs/apply-settings.md)")
    settings_file = Path(args.settings).resolve() if user else settings.SETTINGS / "github.toml"
    before = settings_file.read_bytes()
    s = settings.load_user_settings(settings_file) if user else settings.load_settings("github")
    if user:
        stale = _user_commits(args, s)
        if stale:
            raise GateError(f"{stale}; refusing to apply")
    data = _plan_without_token(args)
    if settings_file.read_bytes() != before:
        raise GateError(f"{settings_file} changed while the plan was computed; refusing to apply")
    s, mod, plans, org = _trusted_plans(data, args.repo, s, Path(args.config))
    token = _token()
    max_minutes = data["(gate)"]["max_minutes"]
    ready, heads = _ready(plans, args.checkouts, s)
    _print_ready(plans, ready, heads)
    owner = s["org"]["owner"]
    in_run = [p for p in plans if not ready[p.name]]

    # 1. Read everything first (GETs only), so nothing is written unless the whole run can go.
    warnings, changes, skipped_org = [], [], []
    ours = {rs["name"] for p in plans for rs in p.rulesets} | {w["ruleset"] for w in org}
    try:
        for p in in_run:
            for line in mod.existing_protection(owner, p.name, ours, token):
                warnings.append(f"{p.name}: {line}")
                print(f"WARNING  {p.name}: {line}", flush=True)
            for c in mod.plan_repo(owner, p.name, list(p.rulesets), token):
                changes.append(c)
                _print_change(c)
            wanted = settings.repo_settings(next(r for r in s["repo"] if r["name"] == p.name))
            for c in mod.plan_repo_settings(owner, p.name, wanted, token):
                changes.append(c)
                _print_change(c)
        if args.org:
            enabled = [w for w in org if w.get("enabled", False)]
            if not enabled:
                print("qqgate: no org ruleset applied: every [[org_workflows]] has enabled = false", file=sys.stderr)
            names = {p.name for p in in_run}
            for w in enabled:
                targets = [t for t in w["targets"] if t in names]
                why = settings.org_workflow_readiness(w, Path(args.checkouts), max_minutes, owner)
                if why:  # the workflow file itself is not safe to require: a real problem, exit 1
                    skipped_org.append(w["ruleset"])
                    print(f"skip     org ruleset {w['ruleset']}: {'; '.join(why)}", flush=True)
                    continue
                if not targets:  # its repos are not in this run (--repo) or not ready (reported above)
                    print(f"skip     org ruleset {w['ruleset']}: none of {', '.join(w['targets'])} is ready in "
                          "this run", flush=True)
                    continue
                c = mod.plan_org(owner, w, targets, token, max_minutes)
                changes.append(c)
                _print_change(c)
    except Exception as e:  # nothing has been written yet
        return _stopped("reading GitHub", e, [], False)
    not_ready = sum(1 for w in ready.values() if w)
    todo = [c for c in changes if c["action"] != "unchanged"]
    shown = _plan_items(warnings, changes)
    if not args.yes:
        if args.save_plan:
            Path(args.save_plan).write_text(json.dumps(sorted(shown)) + "\n")
        print(f"done     dry run: {len(todo)} write(s) planned, {not_ready} repo(s) not ready", flush=True)
        return 1 if not_ready or skipped_org else 0
    if args.expect_plan:
        # The write re-plans from scratch. Write only what the dry run showed: a repo that left the
        # plan (it moved) just drops its writes, but a change or WARNING the dry run did not show (a
        # repo turned ready, a ruleset edited on GitHub) refuses the whole write (re-check E-2).
        try:
            saw = json.loads(Path(args.expect_plan).read_text())
        except (OSError, ValueError) as e:
            raise GateError(f"--expect-plan {args.expect_plan}: cannot read the dry run's plan: {e}") from None
        if not isinstance(saw, list) or not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in saw):
            raise GateError(f"--expect-plan {args.expect_plan}: not a plan saved by --save-plan (a JSON list of hashes)")
        saw = set(saw)
        new = [shown[h] for h in sorted(shown) if h not in saw]
        if new:
            print("REFUSED  nothing written: the plan changed since the dry run, which did not show: "
                  + "; ".join(new) + ". Run the dry run again and review it", flush=True)
            return 1
    refuse = []
    if warnings and not args.accept_warnings:
        refuse.append("WARNING lines above (review them, then add --accept-warnings)")
    differs = [f"{c['where']} {c['name']}" for c in todo if c["action"] == "update" and c.get("kind") != "setting"]
    if differs and not args.overwrite:
        refuse.append(f"live rulesets differ from settings: {', '.join(differs)} (add --overwrite to replace them; "
                      "anything added in the UI, such as a bypass, is removed)")
    if refuse:
        print("REFUSED  nothing written: " + "; ".join(refuse), flush=True)
        return 1

    # 2. Write, printing each line as soon as it is live.
    for i, c in enumerate(todo):
        try:
            print(mod.write(c, token), flush=True)
        except Exception as e:
            return _stopped(f"{c['where']} {c['name']}", e, [f"{x['where']} {x['name']}" for x in todo[i + 1:]], True)
    print(f"done     {len(todo)} write(s), {len(changes) - len(todo)} unchanged, {not_ready} repo(s) not ready",
          flush=True)
    return 1 if not_ready or skipped_org else 0


def _check(args) -> int:
    """`settings check`: read-only, for a user org's settings file. It holds the token, so it reads
    only that file: no config and no infra-config code."""
    if args.settings is None:
        raise GateError("settings check needs --settings FILE (a user org's settings)")
    s, _ = _settings_file(args)
    names = [r["name"] for r in s["repo"]]
    unknown = sorted(set(args.repo or ()) - set(names))
    if unknown:
        raise GateError(f"not in settings: {unknown}")
    names = [n for n in names if not args.repo or n in args.repo]
    token = _token(child_ran=False)
    owner, approvals = s["org"]["owner"], s["main"]["required_approvals"]
    ours = {s["main"]["ruleset"], s["reserved_tags"]["ruleset"]}
    mod = backends.load("github")
    warned = 0
    for name in names:
        for level, line in mod.check_repo(owner, name, ours, approvals, token):
            warned += level == "warning"
            print(f"{'WARNING ' if level == 'warning' else 'ok      '} {name}: {line}", flush=True)
    print(f"done     {warned} warning(s) on {len(names)} repo(s)", flush=True)
    return 1 if warned else 0


def _plan_items(warnings: list[str], changes: list[dict]) -> dict[str, str]:
    """What a dry run shows, item by item: hash -> a short description. A change's hash covers its
    body and differences, so the same name with other content is a new item. `apply --save-plan`
    stores the hashes; `apply --yes --expect-plan` writes only if every item it would act on was shown."""
    def h(x) -> str:
        return hashlib.sha256(json.dumps(x, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    items = {h({"warning": w}): f"WARNING {w}" for w in warnings}
    for c in changes:
        key = {k: c.get(k) for k in ("where", "name", "action", "method", "url", "body", "diff")}
        items[h(key)] = f"{c['where']}: {c['action']} {c['name']}"
    return items


def _stopped(what: str, e: Exception, rest: list[str], wrote: bool) -> int:
    print(f"FAILED   {what}: {e if isinstance(e, GateError) else f'{type(e).__name__}: {e}'}", flush=True)
    if wrote:
        print("         Every line above that starts with a repo name (not 'plan') is already live on GitHub. "
              "Not attempted: "
              f"{', '.join(rest) or 'nothing'}. Re-running is safe: rulesets are created or updated by name.",
              flush=True)
    else:
        print("         Nothing was written.", flush=True)
    return 2


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
    st.add_argument("action", choices=["plan", "verify", "apply", "check"])
    st.add_argument("--config", help="infra-config checkout (pins.toml [infra-config]); with --settings, the "
                                     "qq-config checkout (data only). Not for check")
    st.add_argument("--settings", metavar="FILE", help="a user org's settings instead of gate's own (one-command setup)")
    st.add_argument("--infra-config", metavar="DIR", help="with --settings: infra-config at qq-config's qq.toml pin")
    st.add_argument("--config-commit", metavar="SHA", help="with --settings, verify and apply: qq-config's commit")
    st.add_argument("--repo", action="append", help="limit to these repos (repeatable)")
    st.add_argument("--checkouts", help="directory holding a default-branch checkout of each repo, by name")
    st.add_argument("--yes", action="store_true", help="apply: really write (default is a dry run)")
    st.add_argument("--org", action="store_true", help="apply: also the enabled org rulesets (needs an admin:org token; not needed for repo rulesets)")
    st.add_argument("--accept-warnings", action="store_true", help="apply --yes: write although WARNING lines were printed")
    st.add_argument("--overwrite", action="store_true", help="apply --yes: replace live rulesets of our names that differ")
    st.add_argument("--save-plan", metavar="FILE", help="apply (dry run): save what it showed, for --expect-plan")
    st.add_argument("--expect-plan", metavar="FILE",
                    help="apply --yes: refuse if the write would do anything the dry run's --save-plan FILE did not show")
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
    except NotOnboarded as e:
        print(f"qqgate: {e}", file=sys.stderr)
        if getattr(args, "json", False):
            print(json.dumps({"repo": e.repo, "onboarded": False}, indent=2))
        return 3
    except GateError as e:
        print(f"qqgate: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # a crash is "could not decide" (2), never "refused" (1) or "pass" (0)
        print(f"qqgate: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
