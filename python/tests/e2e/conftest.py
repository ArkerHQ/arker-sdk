from __future__ import annotations

from collections.abc import Callable

import pytest

Reachable = Callable[..., dict[str, bool]]


@pytest.fixture
def reachable() -> Reachable:
    """Which URLs answer with a non-error status, given a function that runs a
    shell command in the sandbox and returns its stdout."""

    def check(run: Callable[[str], str], *urls: str) -> dict[str, bool]:
        script = "; ".join(f"curl -s -o /dev/null -m 8 -w '%{{http_code}}\\n' {url}" for url in urls)
        codes = run(f"{script}; true").split()
        return {url: code.startswith(("2", "3")) for url, code in zip(urls, codes, strict=True)}

    return check
