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
    """Redelivery audit S3: the timing action installs nothing, so queued-at must import and run on
    the standard library alone, with the working directory off the import path."""
    root = Path(__file__).resolve().parents[1]
    action = (root / "timing" / "action.yml").read_text()
    assert "pip install" not in action and "python3 -I " in action
    event = tmp_path / "event.json"
    event.write_text(json.dumps(EVENT))
    (tmp_path / "yaml.py").write_text("raise SystemExit('the working directory was imported')\n")
    code = (
        "import sys\n"
        "for m in ('yaml', 'jsonschema', 'qqsync'): sys.modules[m] = None\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from qqgate.backends import github\n"
        "github._get = lambda url, token: [{'event': 'added_to_merge_queue', 'created_at': '2026-10-04T11:30:00Z'}]\n"
        "from qqgate.cli import main\n"
        "sys.exit(main(['queued-at', '--event', sys.argv[2], '--repository', 'quirq-ai/xo-space', '--no-export']))\n")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(root / "src"), str(event)],
                       cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "2026-10-04T11:30:00Z"
