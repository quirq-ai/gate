"""Typed gate errors. A gate that cannot compute its checks refuses; it never guesses."""


class GateError(Exception):
    """The required checks cannot be computed, or the inputs are inconsistent."""


class NotOnboarded(GateError):
    """The repo is not in infra-config repos.toml, so no gate applies to it (CLI exit 3)."""

    def __init__(self, repo: str, known: list[str]):
        super().__init__(f"{repo!r} is not an onboarded repo in infra-config repos.toml "
                         f"(known: {', '.join(known)})")
        self.repo = repo
