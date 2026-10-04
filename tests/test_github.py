import shutil

from qqgate import backends, required, verdict
from qqgate.backends import github


def test_generated_workflows_produce_every_required_check(cfg, config_root):
    for repo in ("xo-space", "innernet"):
        assert github.check_workflows(config_root, required.compute(cfg, repo)) == []


def test_missing_merge_group_trigger_is_caught(cfg, config_root, tmp_path):
    shutil.copytree(config_root / "generated", tmp_path / "generated")
    wf = tmp_path / "generated" / "github" / "xo-space" / "qq-xo-space-presubmit.yml"
    wf.write_text(wf.read_text().replace("  merge_group:\n", ""))
    problems = github.check_workflows(tmp_path, required.compute(cfg, "xo-space"))
    assert any("merge_group" in p for p in problems)


def test_renamed_job_is_caught(cfg, config_root, tmp_path):
    shutil.copytree(config_root / "generated", tmp_path / "generated")
    wf = tmp_path / "generated" / "github" / "innernet" / "qq-innernet-presubmit.yml"
    wf.write_text(wf.read_text().replace("  innernet-presubmit:\n", "  presubmit:\n"))
    problems = github.check_workflows(tmp_path, required.compute(cfg, "innernet"))
    assert any("no job 'innernet-presubmit'" in p for p in problems)


def test_rule_pins_checks_to_github_actions(cfg):
    rule = github.required_checks_rule(required.compute(cfg, "xo-space"))
    assert rule["type"] == "required_status_checks"
    assert rule["parameters"]["required_status_checks"] == [
        {"context": "xo-space-presubmit", "integration_id": github.GITHUB_ACTIONS_APP_ID}]


WF = ".github/workflows/qq-xo-space-presubmit.yml"
EXPECTED = {"xo-space-presubmit": WF}


def fake_api(monkeypatch, check_runs, workflow_runs):
    def get(url, token):
        if "/actions/runs" in url:
            return {"total_count": len(workflow_runs), "workflow_runs": workflow_runs}
        return {"total_count": len(check_runs), "check_runs": check_runs}
    monkeypatch.setattr(github, "_get", get)


def run(conclusion, started, suite, status="completed"):
    return {"name": "xo-space-presubmit", "conclusion": conclusion, "status": status,
            "started_at": started, "check_suite": {"id": suite}}


def test_observe_takes_newest_run(monkeypatch):
    fake_api(monkeypatch, [run("failure", "2026-10-04T10:00:00Z", 1), run("success", "2026-10-04T11:00:00Z", 1)],
             [{"check_suite_id": 1, "path": WF}])
    assert github.observe("github.com/quirq-ai/xo-space", "abc", EXPECTED, token="") == {"xo-space-presubmit": "success"}


def test_pending_rerun_fails_closed(monkeypatch):
    fake_api(monkeypatch, [run("success", "2026-10-04T10:00:00Z", 1), run(None, None, 2, status="queued")],
             [{"check_suite_id": 1, "path": WF}, {"check_suite_id": 2, "path": WF}])
    assert github.observe("github.com/quirq-ai/xo-space", "abc", EXPECTED, token="") == {"xo-space-presubmit": "queued"}


def test_same_named_check_from_another_workflow_is_a_spoof(monkeypatch, cfg):
    fake_api(monkeypatch, [run("failure", "2026-10-04T10:00:00Z", 1), run("success", "2026-10-04T11:00:00Z", 2)],
             [{"check_suite_id": 1, "path": WF}, {"check_suite_id": 2, "path": ".github/workflows/sneaky.yml"}])
    observed = github.observe("github.com/quirq-ai/xo-space", "abc", EXPECTED, token="")
    assert observed == {"xo-space-presubmit": github.SPOOFED}
    assert not verdict.evaluate(required.compute(cfg, "xo-space"), observed).passed


def test_unreachable_api_is_a_gate_error(monkeypatch):
    import urllib.error
    import urllib.request

    import pytest

    from qqgate.errors import GateError

    def boom(*a, **k):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(github._OPENER, "open", boom)
    with pytest.raises(GateError, match="unreachable"):
        github._get("https://api.github.com/x", None)
    with pytest.raises(GateError, match="unreachable"):
        github._send("GET", "https://api.github.com/x", "t")


def test_token_never_follows_a_redirect(monkeypatch):
    """S4: a redirect is refused, so the token cannot reach another host."""
    import http.server
    import threading

    import pytest

    from qqgate.errors import GateError

    seen = []

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    other = http.server.HTTPServer(("127.0.0.1", 0), Other)

    class Redirect(Other):
        def do_GET(self):
            self.send_response(301)
            self.send_header("Location", f"http://127.0.0.1:{other.server_port}/x")
            self.end_headers()

    first = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    for srv in (first, other):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(github, "API", f"http://127.0.0.1:{first.server_port}")
        for call in (lambda: github._get(f"{github.API}/x", "secret"),
                     lambda: github._send("GET", f"{github.API}/x", "secret")):
            with pytest.raises(GateError, match="301"):
                call()
        assert seen == []
        with pytest.raises(GateError, match="outside"):
            github._get("https://example.com/x", "secret")
    finally:
        first.shutdown(); other.shutdown()


def test_skippable_job_is_caught(cfg, config_root, tmp_path):
    shutil.copytree(config_root / "generated", tmp_path / "generated")
    wf = tmp_path / "generated" / "github" / "xo-space" / "qq-xo-space-presubmit.yml"
    wf.write_text(wf.read_text().replace("    runs-on:", "    if: false\n    runs-on:", 1))
    assert any("can skip" in p for p in github.check_workflows(tmp_path, required.compute(cfg, "xo-space")))


def test_backend_loader():
    assert backends.load("github") is github
