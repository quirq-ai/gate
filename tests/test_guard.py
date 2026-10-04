import pytest

from qqgate import guard
from qqgate.cli import main
from qqgate.errors import GateError


@pytest.fixture
def terms():
    return guard.load_terms()


def core_repo(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_planted_pytest_string_fails(tmp_path, terms):
    root = core_repo(tmp_path, {"src/qqsync/run.py": 'RUNNER = "pytest"\n'})
    [f] = guard.scan_repo(root, "sync", terms)
    assert (f.path, f.line, f.term, f.category, f.where) == ("src/qqsync/run.py", 1, "pytest", "build_tool", "string")


@pytest.mark.parametrize("code,where", [
    ("import pytest\n", "identifier"),
    ("def run_pytest():\n    pass\n", "identifier"),
    ("def runVercel():\n    pass\n", "identifier"),
    ("x = 1  # deploy with vercel\n", "comment"),
    ("cmd = ['npm', 'ci']\n", "string"),
])
def test_python_pieces_are_caught(tmp_path, terms, code, where):
    root = core_repo(tmp_path, {"src/x/m.py": code})
    assert [f.where for f in guard.scan_repo(root, "depot", terms)] == [where]


def test_other_files_are_grepped(tmp_path, terms):
    root = core_repo(tmp_path, {"bin/qq": "#!/bin/sh\nexec pnpm build\n"})
    assert [(f.line, f.term) for f in guard.scan_repo(root, "depot", terms)] == [(2, "pnpm")]


def test_words_inside_other_words_do_not_match(tmp_path, terms):
    root = core_repo(tmp_path, {"src/x/m.py": 'helper = "rusty pipeline javascripts awsome"\n'})
    assert guard.scan_repo(root, "depot", terms) == []


def test_tests_docs_and_ci_are_not_core(tmp_path, terms):
    root = core_repo(tmp_path, {"tests/test_x.py": "import pytest\n", "README.md": "pytest\n",
                                ".github/workflows/ci.yml": "run: pytest\n", "src/x/README.md": "npm\n"})
    assert guard.scan_repo(root, "sync", terms) == []


def test_runtime_is_not_a_finding(tmp_path, terms):
    root = core_repo(tmp_path, {"src/x/m.py": 'cmd = ["python", "-m", "pip", "install"]\n'})
    assert guard.scan_repo(root, "depot", terms) == []


def test_media_types_are_formats_not_targets(tmp_path, terms):
    files = {"src/x/oci.py": 'A = "application/vnd.docker.distribution.manifest.v2+json"\nB = "docker"\n'}
    assert [(f.line, f.term) for f in guard.scan_repo(core_repo(tmp_path, files), "sync", terms)] == [(2, "docker")]


def test_allow_list_is_per_repo_and_path(tmp_path, terms):
    files = {"src/qqgate/guard.py": 'X = "pytest"\n'}
    assert guard.scan_repo(core_repo(tmp_path / "a", files), "gate", terms) == []
    assert [f.term for f in guard.scan_repo(core_repo(tmp_path / "b", files), "sync", terms)] == ["pytest"]


def test_unparseable_code_is_refused(tmp_path, terms):
    root = core_repo(tmp_path, {"src/x/m.py": "def (:\n"})
    with pytest.raises(GateError, match="cannot parse"):
        guard.scan_repo(root, "depot", terms)


def test_non_core_repo_is_refused(tmp_path, terms):
    with pytest.raises(GateError, match="not a core repo"):
        guard.scan_repo(tmp_path, "recipes", terms)


def test_gate_itself_is_agnostic(capsys):
    from tests.conftest import ROOT
    assert main(["guard", "--repo", "gate", str(ROOT)]) == 0


def test_cli_exit_codes(tmp_path):
    root = core_repo(tmp_path, {"src/x/m.py": 'TARGET = "vercel"\n'})
    assert main(["guard", "--repo", "release", str(root)]) == 1
