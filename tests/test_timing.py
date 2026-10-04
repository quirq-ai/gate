import json
import subprocess
import sys
from pathlib import Path

import pytest

from qqgate import timing
from qqgate.backends import github
from qqgate.cli import main
from qqgate.errors import GateError

EVENT = {"merge_group": {
    "head_ref": "refs/heads/gh-readonly-queue/main/pr-212-0123456789abcdef0123456789abcdef01234567",
    "head_commit": {"timestamp": "2026-10-04T14:05:00+02:00"},
}}


def test_rfc3339_normalizes_to_utc():
    assert timing.rfc3339("2026-10-04T14:05:00+02:00") == "2026-10-04T12:05:00Z"
    assert timing.rfc3339("2026-10-04T12:05:00Z") == "2026-10-04T12:05:00Z"
    for bad in ("yesterday", "2026-10-04T12:05:00", None):
        with pytest.raises(GateError):
            timing.rfc3339(bad)


def test_pr_number_from_queue_ref():
    assert github.queue_pr_number(EVENT["merge_group"]["head_ref"]) == 212
    with pytest.raises(GateError):
        github.queue_pr_number("refs/heads/main")


def test_newest_queue_entry_wins(monkeypatch):
    timeline = [{"event": "added_to_merge_queue", "created_at": "2026-10-04T11:00:00Z"},
                {"event": "removed_from_merge_queue", "created_at": "2026-10-04T11:10:00Z"},
                {"event": "added_to_merge_queue", "created_at": "2026-10-04T11:30:00Z"},
                {"event": "commented", "created_at": "2026-10-04T11:40:00Z"}]
    seen = []
    monkeypatch.setattr(github, "_get", lambda url, token: seen.append(url) or timeline)
    q = github.queued_at(EVENT, "quirq-ai/xo-space", token="")
    assert (q.at, q.exact) == ("2026-10-04T11:30:00Z", True)
    assert "/repos/quirq-ai/xo-space/issues/212/timeline" in seen[0]


def test_entry_after_the_group_was_built_is_not_this_runs(monkeypatch):
    # An old group's run still going after the PR was removed and re-added (EVENT built at 12:05).
    timeline = [{"event": "added_to_merge_queue", "created_at": "2026-10-04T12:00:00Z"},
                {"event": "removed_from_merge_queue", "created_at": "2026-10-04T12:10:00Z"},
                {"event": "added_to_merge_queue", "created_at": "2026-10-04T12:20:00Z"}]
    monkeypatch.setattr(github, "_get", lambda url, token: timeline)
    assert github.queued_at(EVENT, "quirq-ai/xo-space", token="").at == "2026-10-04T12:00:00Z"


def test_every_entry_after_the_build_falls_back_inexact(monkeypatch):
    monkeypatch.setattr(github, "_get", lambda url, token: [
        {"event": "added_to_merge_queue", "created_at": "2026-10-04T12:20:00Z"}])
    q = github.queued_at(EVENT, "quirq-ai/xo-space", token="")
    assert (q.at, q.exact) == ("2026-10-04T12:05:00Z", False)


def test_no_group_commit_time_uses_newest_entry(monkeypatch):
    event = {"merge_group": {**EVENT["merge_group"], "head_commit": {}}}
    monkeypatch.setattr(github, "_get", lambda url, token: [
        {"event": "added_to_merge_queue", "created_at": "2026-10-04T11:00:00Z"},
        {"event": "added_to_merge_queue", "created_at": "2026-10-04T12:20:00Z"}])
    q = github.queued_at(event, "quirq-ai/xo-space", token="")
    assert (q.at, q.exact) == ("2026-10-04T12:20:00Z", True)


def test_cli_does_not_export_an_approximate_time(monkeypatch, tmp_path, capsys):
    def down(url, token):
        raise GateError("unreachable")
    monkeypatch.setattr(github, "_get", down)
    event, env = tmp_path / "event.json", tmp_path / "env"
    event.write_text(json.dumps(EVENT))
    monkeypatch.setenv("GITHUB_ENV", str(env))
    assert main(["queued-at", "--event", str(event), "--repository", "quirq-ai/xo-space"]) == 1
    assert not env.exists()
    assert capsys.readouterr().out == ""


def test_falls_back_to_group_commit_time_marked_inexact(monkeypatch):
    def down(url, token):
        raise GateError("unreachable")
    monkeypatch.setattr(github, "_get", down)
    q = github.queued_at(EVENT, "quirq-ai/xo-space", token="")
    assert (q.at, q.exact, q.source) == ("2026-10-04T12:05:00Z", False, "merge_group.head_commit.timestamp")


def test_not_a_gate_run():
    with pytest.raises(GateError, match="not a merge_group"):
        github.queued_at({"pull_request": {}}, "quirq-ai/xo-space", token="")


def test_cli_exports_for_the_sink(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(github, "_get", lambda url, token: [
        {"event": "added_to_merge_queue", "created_at": "2026-10-04T11:30:00Z"}])
    event, env = tmp_path / "event.json", tmp_path / "env"
    event.write_text(json.dumps(EVENT))
    monkeypatch.setenv("GITHUB_ENV", str(env))
    assert main(["queued-at", "--event", str(event), "--repository", "quirq-ai/xo-space"]) == 0
    assert capsys.readouterr().out.strip() == "2026-10-04T11:30:00Z"
    assert env.read_text() == "QQ_QUEUED_AT=2026-10-04T11:30:00Z\n"


def test_action_runs_with_nothing_installed(tmp_path):
    """Redelivery audit S3: the timing action installs nothing. Run its own step script with a
    python3 that has no site-packages, a decoy module in the working directory and a stubbed
    timeline: queued-at must load only the standard library and qqgate."""
    import yaml
    root = Path(__file__).resolve().parents[1]
    action = yaml.safe_load((root / "timing" / "action.yml").read_text())
    script = action["runs"]["steps"][0]["run"]
    assert "pip install" not in script
    event = tmp_path / "event.json"
    event.write_text(json.dumps(EVENT))
    (tmp_path / "yaml.py").write_text("raise SystemExit('the working directory was imported')\n")
    # A python3 shim that stubs the timeline and reports every module loaded from outside the stdlib.
    shim = tmp_path / "bin" / "python3"
    shim.parent.mkdir()
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "qqtest_hook.py").write_text(
        "import atexit, sys\n"
        "def _check():\n"
        "    bad = sorted(m for m in sys.modules if m.split('.')[0] not in sys.stdlib_module_names\n"
        "                 and m.split('.')[0] not in ('qqgate', 'qqtest_hook', '__main__'))\n"
        "    if bad: sys.stderr.write(f'NON-STDLIB {bad}\\n'); sys.stdout.flush(); import os; os._exit(9)\n"
        "atexit.register(_check)\n"
        "def stub():\n"
        "    from qqgate.backends import github\n"
        "    github._get = lambda url, token: [{'event': 'added_to_merge_queue', 'created_at': '2026-10-04T11:30:00Z'}]\n")
    # The shim keeps the action's isolation (-I -S) and runs its -c code with the hook loaded.
    shim.write_text(
        "#!" + sys.executable + " -ISB\n"
        "import sys\n"
        f"sys.path.insert(0, {str(hook)!r})\n"
        "args = sys.argv[1:]\n"
        "assert args[:2] in (['-I', '-S'],), args\n"
        "code, rest = args[args.index('-c') + 1], args[args.index('-c') + 2:]\n"
        "sys.argv = ['-c'] + rest\n"
        "if 'qqgate.cli' in code:\n"
        "    sys.path.insert(0, rest[0])\n"
        "    import qqtest_hook; qqtest_hook.stub()\n"
        "exec(compile(code, '<action>', 'exec'))\n")
    shim.chmod(0o755)
    out = tmp_path / "out"
    env = {"PATH": f"{shim.parent}:/usr/bin:/bin", "GITHUB_ACTION_PATH": str(root / "timing"),
           "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "quirq-ai/xo-space",
           "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(tmp_path / "env"), "QQ_GITHUB_TOKEN": "t"}
    r = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "::warning" not in r.stdout, r.stdout + r.stderr
    assert out.read_text() == "queued-at=2026-10-04T11:30:00Z\n"
    assert (tmp_path / "env").read_text() == "QQ_QUEUED_AT=2026-10-04T11:30:00Z\n"


def test_action_warns_on_an_old_python(tmp_path):
    import yaml
    root = Path(__file__).resolve().parents[1]
    script = yaml.safe_load((root / "timing" / "action.yml").read_text())["runs"]["steps"][0]["run"]
    shim = tmp_path / "python3"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o755)
    r = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                       env={"PATH": f"{tmp_path}:/usr/bin:/bin", "GITHUB_ACTION_PATH": str(root / "timing"),
                            "GITHUB_OUTPUT": str(tmp_path / "out")})
    assert r.returncode == 0 and "needs python3 3.11 or later" in r.stdout
    assert not (tmp_path / "out").exists()
