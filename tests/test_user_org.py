"""A user org's settings (`qqgate settings --settings FILE`, the one-command setup in quirq-ai/setup):
only qq-main and qq-reserved-tags, only for the org its qq-config names, never quirq-ai's."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from qqgate import cli, config, settings
from qqgate.backends import github
from qqgate.cli import main
from qqgate.errors import GateError

ROOT = Path(__file__).resolve().parent.parent
PRODUCTS = ("xo-space", "website", "innernet")
QQ_CONFIG = ('[[infra_repo]]\nname = "qq-config"\nsource = "github.com/acme/qq-config"\n'
             'visibility = "public"\nwave = 1\nowns = "acme\'s qq config."\nchromium = "infra/config"\n'
             'owners = []\n\n')


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main",
                    *args], cwd=cwd, check=True, capture_output=True)


def _rev(cwd) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture(scope="session")
def acme(tmp_path_factory, config_root):
    """A data-only qq-config for github.com/acme, as infra-config's own UserOrg test builds it, generated
    by the pinned infra-config's qqcfg, committed, with origin https://github.com/acme/qq-config."""
    d = tmp_path_factory.mktemp("acme") / "qq-config"
    shutil.copytree(config_root / "config", d / "config")
    org = d / "config/org.toml"
    text = org.read_text().replace('code_host = "github.com/quirq-ai"', 'code_host = "github.com/acme"', 1)
    first, last = text.index("[[infra_repo]]"), text.rindex("[[infra_repo]]")
    rest = text[last:]
    rest = rest[re.search(r"\n\n(?=[#\[])", rest).end():]
    org.write_text(text[:first] + QQ_CONFIG + rest)
    repos = d / "config/repos.toml"
    repos.write_text(repos.read_text().replace('source = "github.com/quirq-ai/', 'source = "github.com/acme/'))
    subprocess.run([sys.executable, str(config_root / "tools/qqcfg.py"), "generate", "--root", str(d)],
                   check=True, capture_output=True)
    (d / "qq.toml").write_text(f'[infra-config]\ncommit = "{"0" * 40}"\n')
    _git("init", "-q", cwd=d)
    _git("add", "-A", cwd=d)
    _git("commit", "-qm", "qq-config", cwd=d)
    _git("remote", "add", "origin", "https://github.com/acme/qq-config", cwd=d)
    return d


SETTINGS = """[org]
owner = "acme"

[main]
merge_method = "squash"
required_approvals = 0

[reserved_tags]
names = ["main"]

[[repo]]
name = "qq-config"
kind = "infra"
checks = ["presubmit"]
""" + "".join(f'\n[[repo]]\nname = "{p}"\nkind = "product"\n' for p in PRODUCTS)


@pytest.fixture
def user_file(tmp_path):
    def write(text=SETTINGS):
        f = tmp_path / "acme.toml"
        f.write_text(text)
        return f
    return write


def _plan(acme, config_root, f, capsys):
    rc = main(["settings", "plan", "--settings", str(f), "--config", str(acme), "--infra-config", str(config_root)])
    out = capsys.readouterr()
    return rc, (json.loads(out.out) if rc == 0 else out.err)


def test_quirq_plan_is_byte_identical(config_root, capsys):
    """quirq-ai's `settings plan` must not change by one byte with user-org support (spec d). After a
    deliberate change to settings/github.toml or the infra-config pin, regenerate the golden file with
    `qqgate settings plan --config <infra-config at the pin> > tests/golden/quirq-settings-plan.json`."""
    assert main(["settings", "plan", "--config", str(config_root)]) == 0
    assert capsys.readouterr().out == (ROOT / "tests/golden/quirq-settings-plan.json").read_text()


def test_apply_sh_never_passes_the_user_org_flags():
    text = (ROOT / "scripts/apply.sh").read_text()
    for flag in ("--settings", "--infra-config", "--config-commit"):
        assert flag not in text


def test_gate_own_settings_must_keep_release_refs(tmp_path, monkeypatch):
    text = (settings.SETTINGS / "github.toml").read_text()
    start = text.index("[release_refs]")
    (tmp_path / "github.toml").write_text(text[:start] + text[text.index("[reserved_tags]"):])
    monkeypatch.setattr(settings, "SETTINGS", tmp_path)
    with pytest.raises(GateError, match=r"\[release_refs\] is missing"):
        settings.load_settings("github")


def test_user_plan_is_only_qq_main_and_reserved_tags(acme, config_root, user_file, capsys):
    rc, plan = _plan(acme, config_root, user_file(), capsys)
    assert rc == 0, plan
    assert plan["(org)"] == []
    assert sorted(k for k in plan if not k.startswith("(")) == sorted(("qq-config",) + PRODUCTS)
    for name in ("qq-config",) + PRODUCTS:
        rulesets = plan[name]["rulesets"]
        assert [r["name"] for r in rulesets] == ["qq-main", "qq-reserved-tags"]
        assert all(r["bypass_actors"] == [] for r in rulesets)
        pr = next(r for r in rulesets[0]["rules"] if r["type"] == "pull_request")
        assert pr["parameters"]["required_approving_review_count"] == 0
    assert plan["qq-config"]["required"] == ["presubmit"]
    assert all(plan[p]["required"] for p in PRODUCTS)


def test_template_is_valid_user_settings(acme, config_root, user_file, capsys):
    template = (ROOT / "templates/user-org-settings.toml").read_text()
    s = settings.load_user_settings(ROOT / "templates/user-org-settings.toml")
    assert s["org"]["owner"] == "acme" and s["main"]["required_approvals"] == 0
    assert [r["name"] for r in s["repo"] if r["kind"] == "infra"] == ["qq-config"]
    filled = template.replace('[[repo]]\nname = "app"\nkind = "product"\n',
                              "".join(f'[[repo]]\nname = "{p}"\nkind = "product"\n\n' for p in PRODUCTS))
    rc, plan = _plan(acme, config_root, user_file(filled), capsys)
    assert rc == 0, plan


@pytest.mark.parametrize("owner", ["quirq-ai", "Quirq-AI", "QUIRQ-AI"])
def test_quirq_ai_is_refused_in_any_case(user_file, owner):
    with pytest.raises(GateError, match="settings/github.toml"):
        settings.load_user_settings(user_file(SETTINGS.replace('owner = "acme"', f'owner = "{owner}"')))


@pytest.mark.parametrize("section,name", [("main", "qq-main-2"), ("reserved_tags", "other"),
                                          ("main", "qq-reserved-tags")])
def test_ruleset_names_are_fixed(user_file, section, name):
    text = SETTINGS.replace(f"[{section}]\n", f'[{section}]\nruleset = "{name}"\n')
    with pytest.raises(GateError, match="ruleset must be"):
        settings.load_user_settings(user_file(text))
    ok = SETTINGS.replace(f"[{section}]\n", f'[{section}]\nruleset = "{settings.USER_RULESETS[section]}"\n')
    settings.load_user_settings(user_file(ok))


def test_owner_must_be_the_code_host_owner(acme, config_root, user_file, capsys):
    rc, err = _plan(acme, config_root, user_file(SETTINGS.replace('owner = "acme"', 'owner = "other"')), capsys)
    assert rc == 2 and "code_host" in err
    rc, plan = _plan(acme, config_root, user_file(SETTINGS.replace('owner = "acme"', 'owner = "ACME"')), capsys)
    assert rc == 0, plan   # GitHub owner names ignore case


@pytest.mark.parametrize("extra,why", [
    ('[release_refs]\nruleset = "qq-release-refs"\nbranches = []\ntags = []\nbypass_integration_ids = [1]\n',
     "not allowed"),
    ('[dependabot]\nruleset = "x"\nbranches = "refs/heads/dependabot/**/*"\nactor_id = 1\n', "not allowed"),
    ('[[org_workflows]]\nruleset = "x"\n', "not allowed"),
    ('[gate]\nx = 1\n', "not allowed"),
])
def test_forbidden_sections_are_refused(user_file, extra, why):
    with pytest.raises(GateError, match=why):
        settings.load_user_settings(user_file(SETTINGS + "\n" + extra))


@pytest.mark.parametrize("old,new,why", [
    ('name = "xo-space"\nkind = "product"\n', 'name = "xo-space"\nkind = "product"\nallow_auto_merge = true\n', "keys"),
    ('name = "xo-space"\nkind = "product"\n', 'name = "xo-space"\nkind = "product"\nrequired_approvals = 0\n', "keys"),
    ('name = "xo-space"\nkind = "product"\n', 'name = "xo-space"\nkind = "product"\nstate_branches = ["x"]\n', "keys"),
    ('name = "xo-space"\nkind = "product"\n', 'name = "xo-space"\nkind = "infra"\n', "kind must be"),
    ('name = "qq-config"\nkind = "infra"\n', 'name = "qq-config"\nkind = "product"\n', "kind must be"),
    ('checks = ["presubmit"]\n', 'checks = ["presubmit"]\ncode_owner_review = true\n', "keys"),
    ('required_approvals = 0\n', 'required_approvals = 0\nbypass = [{actor_id = 1}]\n', "bypass"),
    ('required_approvals = 0\n', 'required_approvals = 0\nextra = 1\n', "keys"),
    ('required_approvals = 0\n', 'required_approvals = 11\n', "0 to 10"),
    ('owner = "acme"\n', 'owner = "acme"\nfoo = 1\n', "keys"),
    ('owner = "acme"\n', 'owner = "ac me"\n', "GitHub org name"),
    ('names = ["main"]\n', 'names = []\n', "non-empty"),
])
def test_forbidden_keys_and_values_are_refused(user_file, old, new, why):
    assert old in SETTINGS
    with pytest.raises(GateError, match=why):
        settings.load_user_settings(user_file(SETTINGS.replace(old, new, 1)))


def test_org_and_stray_flags_are_refused(acme, config_root, user_file, tmp_path, capsys):
    f = user_file()
    base = ["--settings", str(f), "--config", str(acme), "--infra-config", str(config_root)]
    assert main(["settings", "apply", *base, "--checkouts", str(tmp_path), "--org"]) == 2
    assert "--org is not available" in capsys.readouterr().err
    assert main(["settings", "plan", "--config", str(acme), "--infra-config", str(config_root)]) == 2
    assert "go with --settings" in capsys.readouterr().err
    assert main(["settings", "plan", "--settings", str(f), "--config", str(acme)]) == 2
    assert "needs --infra-config" in capsys.readouterr().err


# --- commit checks (spec e) ----------------------------------------------------------------------

@pytest.fixture
def commits(tmp_path, monkeypatch, acme):
    """A qq-config clone at a known commit, and an infra-config clone whose origin is a local bare
    repo standing in for quirq-ai/infra-config (pins.toml's source), at the commit qq.toml pins."""
    bare = tmp_path / "infra-config.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    ic = tmp_path / "infra-config"
    _git("clone", "-q", str(bare), str(ic), cwd=tmp_path)
    (ic / "README").write_text("x")
    _git("add", "-A", cwd=ic)
    _git("commit", "-qm", "c", cwd=ic)
    _git("push", "-q", "origin", "HEAD:main", cwd=ic)
    pin = _rev(ic)
    monkeypatch.setattr(cli, "_infra_config_source", lambda: str(bare))
    data = tmp_path / "qq-config"
    shutil.copytree(acme, data)
    (data / "qq.toml").write_text(f'[infra-config]\ncommit = "{pin}"\n')
    _git("commit", "-qam", "pin", cwd=data)
    return data, ic, _rev(data)


def _args(data, ic, commit, f):
    return type("A", (), {"config": str(data), "infra_config": str(ic), "config_commit": commit,
                          "settings": str(f)})()


def test_commit_checks_pass_for_a_clean_pinned_pair(commits, user_file):
    data, ic, head = commits
    s = settings.load_user_settings(user_file())
    assert cli._user_commits(_args(data, ic, head, ""), s) is None
    _git("remote", "set-url", "origin", "https://github.com/ACME/qq-config.git", cwd=data)
    assert cli._user_commits(_args(data, ic, head, ""), s) is None   # owner names ignore case


@pytest.mark.parametrize("breaks,why", [
    (lambda data, ic, head: (None, "nope"), "--config-commit"),
    (lambda data, ic, head: (None, "1" * 40), "not --config-commit"),
    (lambda data, ic, head: ((data / "config/org.toml").write_text("x"), head), "local changes"),
    (lambda data, ic, head: ((data / "config/extra.toml").write_text("x"), head), "untracked"),
    (lambda data, ic, head: (_git("remote", "set-url", "origin", "https://github.com/other/qq-config", cwd=data),
                             head), "was not cloned from https://github.com/acme/qq-config"),
    (lambda data, ic, head: ((ic / "README").write_text("y"), head), "--infra-config .* local changes"),
    (lambda data, ic, head: (_git("remote", "set-url", "origin", "https://github.com/x/infra-config", cwd=ic),
                             head), "--infra-config .* was not cloned from"),
])
def test_commit_checks_refuse(commits, user_file, breaks, why):
    data, ic, head = commits
    _, commit = breaks(data, ic, head)
    assert re.search(why, cli._user_commits(_args(data, ic, commit, ""), settings.load_user_settings(user_file())))


def test_commit_checks_refuse_infra_config_off_its_pin_or_off_main(commits, user_file):
    data, ic, head = commits
    s = settings.load_user_settings(user_file())
    (ic / "README").write_text("side")
    _git("commit", "-qam", "side", cwd=ic)   # not pushed: not on main, and not qq.toml's pin
    assert "is not at qq.toml's pin" in cli._user_commits(_args(data, ic, head, ""), s)
    (data / "qq.toml").write_text(f'[infra-config]\ncommit = "{_rev(ic)}"\n')
    _git("commit", "-qam", "pin side", cwd=data)
    assert "is not on" in cli._user_commits(_args(data, ic, _rev(data), ""), s)


def test_commit_checks_refuse_a_code_host_of_another_org(commits, user_file):
    data, ic, head = commits
    org = data / "config/org.toml"
    org.write_text(org.read_text().replace('code_host = "github.com/acme"', 'code_host = "github.com/other"', 1))
    _git("commit", "-qam", "other", cwd=data)
    assert "code_host" in cli._user_commits(_args(data, ic, _rev(data), ""), settings.load_user_settings(user_file()))


# --- apply and the plan child (spec f) -----------------------------------------------------------

def test_plan_child_gets_the_settings_file_the_parent_checked(acme, config_root, user_file, monkeypatch):
    f = user_file()
    seen = []

    def run(cmd, **k):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")
    monkeypatch.setattr(cli.subprocess, "run", run)
    cli._plan_without_token(type("A", (), {"config": str(acme), "settings": str(f), "infra_config": str(config_root),
                                           "config_commit": None, "repo": None, "no_validate": False})())
    cmd = seen[0]
    assert cmd[cmd.index("--settings") + 1] == str(f.resolve())
    assert cmd[cmd.index("--infra-config") + 1] == str(config_root.resolve())
    assert cmd[cmd.index("--config") + 1] == str(acme.resolve())


@pytest.fixture
def user_apply(monkeypatch, acme, config_root, user_file, tmp_path):
    f = user_file()
    monkeypatch.setenv("QQ_GITHUB_TOKEN", "t")
    monkeypatch.setattr(cli, "_user_commits", lambda args, s: None)
    monkeypatch.setattr(settings, "checkout_state", lambda co, origin=None: settings.CheckoutState("a" * 40, "main", ()))
    monkeypatch.setattr(settings, "readiness", lambda *a: [])
    monkeypatch.setattr(github, "existing_protection", lambda *a: [])
    monkeypatch.setattr(github, "plan_repo_settings", lambda *a: [])

    def child(args):
        cfg, backend, s, plans = cli._plans(args)
        return json.loads(json.dumps(cli._plan_json(cfg, backend, plans, settings.org_workflows(s, cfg))))
    monkeypatch.setattr(cli, "_plan_without_token", child)
    for r in ("qq-config",) + PRODUCTS:
        (tmp_path / "co" / r / ".git").mkdir(parents=True)
    return f, ["settings", "apply", "--settings", str(f), "--config", str(acme), "--infra-config", str(config_root),
               "--config-commit", "0" * 40, "--checkouts", str(tmp_path / "co")]


def test_user_apply_plans_only_its_own_rulesets(user_apply, monkeypatch, capsys):
    _, argv = user_apply
    monkeypatch.setattr(github, "plan_repo", lambda owner, repo, wanted, token: [
        {"where": f"{owner}/{repo}", "name": rs["name"], "action": "create", "diff": []} for rs in wanted])
    assert main(argv) == 0
    out = capsys.readouterr().out
    lines = sorted(line for line in out.splitlines() if line.startswith("plan "))
    assert lines == sorted(f"plan     acme/{r}: create ruleset {n}" for r in ("qq-config",) + PRODUCTS
                           for n in ("qq-main", "qq-reserved-tags"))
    assert "done     dry run: 8 write(s) planned, 0 repo(s) not ready" in out


def test_user_apply_refuses_a_settings_file_changed_during_the_plan(user_apply, monkeypatch, capsys):
    f, argv = user_apply
    child = cli._plan_without_token

    def tamper(args):
        f.write_text(f.read_text().replace("required_approvals = 0", "required_approvals = 1"))
        return child(args)
    monkeypatch.setattr(cli, "_plan_without_token", tamper)
    assert main(argv) == 2
    assert "changed while the plan was computed" in capsys.readouterr().err


def test_user_apply_refuses_failed_commit_checks(user_apply, monkeypatch, capsys):
    _, argv = user_apply
    monkeypatch.setattr(cli, "_user_commits", lambda args, s: "qq.toml's pin is not on main")
    assert main(argv) == 2
    assert "qq.toml's pin is not on main; refusing to apply" in capsys.readouterr().err


# --- settings check ------------------------------------------------------------------------------

RECORDED = {
    "/repos/acme/xo-space": {"default_branch": "main", "visibility": "public", "allow_squash_merge": False},
    "/repos/acme/xo-space/branches/main/protection": {
        "required_status_checks": {"contexts": ["old-ci"]},
        "required_pull_request_reviews": {"required_approving_review_count": 1}},
    "/repos/acme/xo-space/rulesets?includes_parents=true&per_page=100": [{"id": 1, "name": "qq-main"},
                                                                         {"id": 2, "name": "legacy"}],
    "/repos/acme/xo-space/rules/branches/main?per_page=100": [
        {"type": "pull_request", "ruleset_id": 2, "parameters": {"required_approving_review_count": 2}},
        {"type": "pull_request", "ruleset_id": 1, "parameters": {"required_approving_review_count": 0}}],
    "/repos/acme/xo-space/actions/permissions": {"enabled": True, "allowed_actions": "local_only"},
    "/repos/acme/xo-space/actions/permissions/workflow": {"default_workflow_permissions": "read",
                                                          "can_approve_pull_request_reviews": True},
}
CLEAN = {
    "/repos/acme/website": {"default_branch": "main", "visibility": "public", "allow_squash_merge": True},
    "/repos/acme/website/rulesets?includes_parents=true&per_page=100": [],
    "/repos/acme/website/rules/branches/main?per_page=100": [],
    "/repos/acme/website/actions/permissions": {"enabled": True, "allowed_actions": "all"},
    "/repos/acme/website/actions/permissions/workflow": {"default_workflow_permissions": "read",
                                                         "can_approve_pull_request_reviews": False},
}


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setenv("QQ_GITHUB_TOKEN", "t")

    def forbidden(*a, **k):
        raise AssertionError("settings check loaded config or infra-config code")
    monkeypatch.setattr(config, "load", forbidden)
    monkeypatch.setattr(config, "qqcfg_module", forbidden)

    def send(method, url, token, body=None):
        calls.append((method, url))
        path = url.removeprefix(github.API)
        if path in RECORDED or path in CLEAN:
            return {**RECORDED, **CLEAN}[path]
        if path.endswith("/actions/permissions") and "innernet" in path:
            raise GateError(f"GitHub API GET {url}: 403 Forbidden: needs admin")
        raise GateError(f"GitHub API GET {url}: 404 Not Found: {{}}")
    monkeypatch.setattr(github, "_send", send)
    return calls


def _check(f, *repos):
    return main(["settings", "check", "--settings", str(f)] + [a for r in repos for a in ("--repo", r)])


def test_check_reports_every_item_with_gets_only(recorded, user_file, capsys):
    assert _check(user_file(SETTINGS.replace("required_approvals = 0", "required_approvals = 1")), "xo-space") == 1
    out = capsys.readouterr().out
    assert recorded and all(m == "GET" for m, _ in recorded)
    for want in ("ok       xo-space: visibility public",
                 "WARNING  xo-space: squash merging is turned off",
                 "WARNING  xo-space: classic branch protection on main (required checks: old-ci)",
                 "WARNING  xo-space: classic branch protection on main requires pull request reviews (1 approving)",
                 "WARNING  xo-space: other rulesets apply too: legacy",
                 "WARNING  xo-space: ruleset legacy requires 2 approving review(s) on main",
                 "ok       xo-space: ruleset qq-main requires 0 approving review(s) on main",
                 "WARNING  xo-space: Actions allows only local_only actions",
                 "ok       xo-space: workflow token read, can approve pull requests: True",
                 "WARNING  xo-space: GitHub Actions can approve pull requests"):
        assert want in out, want
    assert "WARNING  QQ_GITHUB_TOKEN" not in out   # check runs no infra-config code, so no child to warn about


def test_check_shows_unreadable_items_as_warnings(recorded, user_file, capsys):
    assert _check(user_file(), "innernet") == 1
    out = capsys.readouterr().out
    assert "WARNING  innernet: could not read the repo: GitHub API GET" in out
    assert "WARNING  innernet: could not read its Actions permissions: GitHub API GET" in out and "403" in out
    assert "WARNING  innernet: could not read its rulesets" in out


def test_check_of_a_clean_repo_passes(recorded, user_file, capsys):
    assert _check(user_file(), "website") == 0
    out = capsys.readouterr().out
    assert "ok       website: no classic branch protection on main" in out
    assert "ok       website: Actions on, all actions allowed" in out
    assert "done     0 warning(s) on 1 repo(s)" in out


@pytest.mark.parametrize("text,why", [
    (SETTINGS.replace('owner = "acme"', 'owner = "Quirq-AI"'), "settings/github.toml"),
    (SETTINGS + '\n[dependabot]\nruleset = "x"\n', "not allowed"),
])
def test_check_refuses_quirq_ai_and_forbidden_sections(recorded, user_file, capsys, text, why):
    assert _check(user_file(text)) == 2
    assert why in capsys.readouterr().err and not recorded


def test_check_needs_a_user_settings_file(recorded, capsys):
    assert main(["settings", "check"]) == 2
    assert "needs --settings" in capsys.readouterr().err and not recorded
