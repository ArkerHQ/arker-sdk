from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
TEXT_EXAMPLES = tuple(
    path
    for path in EXAMPLES_DIR.rglob("*")
    if path.is_file()
    and path.suffix in {".html", ".js", ".jsx", ".md", ".mjs", ".py", ".sh"}
)


def test_cli_install_references_use_standalone_package() -> None:
    install_references = []
    for path in TEXT_EXAMPLES:
        source = path.read_text()
        assert "bun add --global @arker-ai/sdk" not in source, path
        if "bun add --global" in source:
            install_references.append((path, source))

    assert install_references
    for path, source in install_references:
        assert "bun add --global @arker-ai/cli" in source, path


def test_run_options_precede_remote_command() -> None:
    trailing_option = re.compile(
        r"arker run\s+(?!-)(?:\"[^\"]+\"|'[^']+'|\S+)\s+"
        r"(?:\"[^\"]*\"|'[^']*')\s+--[a-z]"
    )

    for path in TEXT_EXAMPLES:
        source = path.read_text()
        assert "arker run --background" not in source, path
        assert trailing_option.search(source) is None, path


def test_run_status_piped_to_jq_uses_json_output() -> None:
    for path in TEXT_EXAMPLES:
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if "arker runs get" in line and "jq" in line:
                assert "arker runs get --json " in line, f"{path}:{line_number}"


def test_policies_example_uses_cli_policy_commands() -> None:
    source = (EXAMPLES_DIR / "policies" / "quick-start-policies.sh").read_text()

    assert "arker policies set" in source
    assert "arker policies get" in source
    assert "curl -fsS -X PUT" not in source
    assert "ARKER_REGION" not in source


def test_browser_example_does_not_wait_for_window_manager_activation() -> None:
    source = (EXAMPLES_DIR / "browser" / "quick-start-browser.sh").read_text()

    assert "windowactivate --sync" not in source
    assert 'arker run --time-to-background 0 "$VM" "open-url' in source
    assert "curl -fsS -X PATCH" not in source
    assert 'arker policies set "$ck"' in source


def test_gpu_coding_agents_quickstart_installs_python_sdk() -> None:
    source = (EXAMPLES_DIR / "gpu-coding-agents" / "README.md").read_text()

    assert "uv run --with arker python launch.py" in source


def test_autoresearch_bounds_agent_output_tokens() -> None:
    source = (EXAMPLES_DIR / "autoresearch" / "autoresearch.py").read_text()

    assert '"maxTokens": 16000' in source
    assert 'raise SystemExit(f"missing env: {name}")' in source
