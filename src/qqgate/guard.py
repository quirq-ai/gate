"""V0-GAT-02: the agnosticism guard. No core file names a language, build tool or deploy target.

Python files are read with `ast` (identifiers, imports, attribute names and string literals) and
`tokenize` (comments); every other file is grepped. Terms match as whole words, ignoring case. The
term list, the core repos and the reviewed exceptions live in guard/terms.toml, which only a policy
change can edit: there is no inline pragma.
"""
from __future__ import annotations

import ast
import fnmatch
import io
import re
import tokenize
import tomllib
from dataclasses import dataclass
from pathlib import Path

from qqgate.errors import GateError

TERMS = Path(__file__).resolve().parents[2] / "guard" / "terms.toml"


@dataclass(frozen=True)
class Finding:
    path: str      # repo-relative
    line: int
    term: str
    category: str
    where: str     # identifier | string | comment | text

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: names the {self.category.replace('_', ' ')} {self.term!r} ({self.where})"


def load_terms(path: Path | None = None) -> dict:
    with (path or TERMS).open("rb") as f:
        return tomllib.load(f)


def _matcher(terms: dict) -> re.Pattern:
    runtime = {t.lower() for t in terms.get("runtime", [])}
    words = sorted({t.lower() for ts in terms["terms"].values() for t in ts} - runtime, key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(w) for w in words) + r")(?![A-Za-z0-9])", re.I)


def _category(terms: dict) -> dict[str, str]:
    return {t.lower(): cat for cat, ts in terms["terms"].items() for t in ts}


def _pieces_py(text: str, path: str) -> list[tuple[int, str, str]]:
    """(line, kind, text) for every identifier, string literal and comment in a Python file."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as e:
        raise GateError(f"{path}: cannot parse ({e.msg} at line {e.lineno}); the guard refuses unreadable code") from None
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((line, "string", node.value))
        elif isinstance(node, ast.Name):
            out.append((line, "identifier", node.id))
        elif isinstance(node, ast.Attribute):
            out.append((line, "identifier", node.attr))
        elif isinstance(node, ast.alias):
            out.append((line, "identifier", node.name))
            if node.asname:
                out.append((line, "identifier", node.asname))
        elif isinstance(node, (ast.ImportFrom,)) and node.module:
            out.append((line, "identifier", node.module))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((line, "identifier", node.name))
        elif isinstance(node, ast.arg):
            out.append((line, "identifier", node.arg))
        elif isinstance(node, ast.keyword) and node.arg:
            out.append((line, "identifier", node.arg))
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            out.append((tok.start[0], "comment", tok.string))
    return out


# Registered media types name formats, not deploy targets: application/vnd.docker.distribution...
MEDIA_TYPE = re.compile(r"\b[a-z]+/vnd\.[A-Za-z0-9.+_-]+")


def _split_identifier(s: str) -> str:
    """'run_pytest' and 'runPytest' -> 'run pytest' so whole-word matching sees the term."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s).replace("_", " ")


def scan_file(path: Path, rel: str, terms: dict) -> list[Finding]:
    rx, cat = _matcher(terms), _category(terms)
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        return []  # binary files name nothing
    if path.suffix == ".py":
        pieces = [(ln, kind, _split_identifier(t) if kind == "identifier" else t)
                  for ln, kind, t in _pieces_py(text, rel)]
    else:
        pieces = [(i, "text", line) for i, line in enumerate(text.splitlines(), 1)]
    found = set()
    for ln, kind, t in pieces:
        for m in rx.finditer(MEDIA_TYPE.sub(" ", t)):
            term = m.group(1).lower()
            found.add(Finding(rel, ln, term, cat[term], kind))
    return sorted(found, key=lambda f: (f.line, f.term, f.where))


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def scan_repo(root: Path, repo: str, terms: dict) -> list[Finding]:
    if repo not in terms["core"]:
        raise GateError(f"{repo!r} is not a core repo (guard/terms.toml core = {terms['core']})")
    allowed = {(a["path"], a["term"].lower()) for a in terms.get("allow", []) if a["repo"] == repo}
    wildcard = {path for path, term in allowed if term == "*"}
    findings = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        if not _matches(rel, terms["scan"]) or _matches(rel, terms["skip"]):
            continue
        if rel in wildcard:
            continue
        findings += [f for f in scan_file(p, rel, terms) if (f.path, f.term) not in allowed]
    return findings
