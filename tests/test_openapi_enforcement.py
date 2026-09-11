from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGED_PATHS = (
    Path("openapi.json"),
    Path("typescript/src/generated/api-types.ts"),
    Path("python/src/arker/generated/api_models.py"),
)
TYPESCRIPT_PATH = MANAGED_PATHS[1]
PYTHON_PATH = MANAGED_PATHS[2]


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def annotations(source: str, class_name: str) -> dict[str, str]:
    module = ast.parse(source)
    class_node = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.target.id: ast.unparse(node.annotation)
        for node in class_node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def test_generation_is_deterministic_for_both_languages() -> None:
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        for output in (first, second):
            run(
                "./scripts/generate-openapi",
                "--contract",
                "openapi.json",
                "--output-root",
                output,
            )

        for relative_path in (TYPESCRIPT_PATH, PYTHON_PATH):
            first_bytes = (Path(first) / relative_path).read_bytes()
            second_bytes = (Path(second) / relative_path).read_bytes()
            assert first_bytes == second_bytes

        typescript = (Path(first) / TYPESCRIPT_PATH).read_text()
        python = (Path(first) / PYTHON_PATH).read_text()
        assert "export interface operations" in typescript
        assert "@dataclass(frozen=True)\nclass ForkRequest" in python
        assert "class ForkRequest" in python
        assert "class ListVmsParameters" in python

        operation_ids = {
            operation["operationId"]
            for path_item in json.loads(
                (REPO_ROOT / "openapi.json").read_text()
            )["paths"].values()
            for method, operation in path_item.items()
            if method != "parameters"
        }
        operation_classes = {
            node.name.removesuffix("Operation")
            for node in ast.parse(python).body
            if isinstance(node, ast.ClassDef) and node.name.endswith("Operation")
        }
        assert operation_classes == {
            operation_id[0].upper() + operation_id[1:] for operation_id in operation_ids
        }

        assert annotations(python, "ForkOperation") == {
            "operation_id": "Literal['fork']",
            "method": "Literal['POST']",
            "path": "Literal['/v1/fork']",
            "parameters": "ForkParameters",
            "request": "ForkRequest",
            "success": "Vm",
            "errors": "ErrorResponse",
        }
        assert annotations(python, "PatchSessionOperation") == {
            "operation_id": "Literal['patchSession']",
            "method": "Literal['PATCH']",
            "path": "Literal['/v1/vms/{id}/sessions/{sid}']",
            "parameters": "PatchSessionParameters",
            "request": "PatchSessionRequest | None",
            "success": "PatchSessionResponse",
            "errors": "ErrorResponse",
        }
        assert (
            annotations(python, "CreateSessionOperation")["request"]
            == "CreateSessionRequest"
        )
        assert annotations(python, "SyncOperation")["request"] == "SyncRequest"
        assert annotations(python, "SyncOperation")["success"] == "SyncResponse"


def test_public_wire_types_are_generated() -> None:
    schemas = set(
        json.loads((REPO_ROOT / "openapi.json").read_text())["components"][
            "schemas"
        ]
    )

    typescript = (REPO_ROOT / "typescript/src/index.ts").read_text()
    declarations = dict(
        re.findall(r"^export type (\w+) = ([^;]+);", typescript, re.MULTILINE)
    )
    for name in schemas & declarations.keys():
        assert declarations[name].strip() == f'ApiSchema<"{name}">', name

    python_module = ast.parse((REPO_ROOT / "python/src/arker/computer.py").read_text())
    handwritten_classes = {
        node.name for node in python_module.body if isinstance(node, ast.ClassDef)
    }
    assert schemas.isdisjoint(handwritten_classes), sorted(
        schemas & handwritten_classes
    )
    assert any(
        isinstance(node, ast.ImportFrom) and node.module == "generated.api_models"
        for node in python_module.body
    )

    handwritten_typescript_wire_interfaces = set(
        re.findall(
            r"^(?:export )?interface (\w+(?:Request|Response|Parameters))\b",
            typescript,
            re.MULTILINE,
        )
    )
    assert handwritten_typescript_wire_interfaces == {"TransportResponse"}
    assert "import type { components, operations }" in typescript

    called_python_models = {
        node.func.id
        for node in ast.walk(python_module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "ListFilesystemsParameters",
        "ListOrgRunsParameters",
        "ListRunsParameters",
        "ListSessionsParameters",
        "ListMountsParameters",
        "ListVmsParameters",
        "PatchSessionRequest",
        "PatchVmRequest",
        "RunRequest",
        "MountCreateRequest",
        "SyncReadOperationRequest",
        "SyncWriteOperationRequest",
    }.issubset(called_python_models)


def test_sdk_runtime_uses_only_current_public_error_codes() -> None:
    retired_codes = {
        "api_worker_unavailable",
        "backend_unavailable",
        "network_error",
        "routing_unavailable",
        "temporarily_unavailable",
    }
    for relative_path in (
        Path("python/src/arker/computer.py"),
        Path("typescript/src/index.ts"),
    ):
        source = (REPO_ROOT / relative_path).read_text()
        assert retired_codes.isdisjoint(source.split('"')), relative_path


def test_retired_vm_pin_is_not_in_the_public_sdk_contract() -> None:
    field = "_".join(("keep", "alive"))
    contract = json.loads((REPO_ROOT / "openapi.json").read_text())
    schemas = contract["components"]["schemas"]

    assert field not in schemas["Vm"]["properties"]
    assert field not in schemas["RunRequest"]["properties"]

    for relative_path in (
        Path("python/src/arker/generated/api_models.py"),
        Path("typescript/src/generated/api-types.ts"),
    ):
        assert field not in (REPO_ROOT / relative_path).read_text()


def test_provider_and_region_contract_values_are_open_ended() -> None:
    contract = json.loads((REPO_ROOT / "openapi.json").read_text())
    schemas = contract["components"]["schemas"]

    # `examples` is documentation, not validation: it names the providers we
    # serve today without closing the set, so codegen still emits a plain string
    # and a provider added after a client's release cannot make it reject a
    # response. `enum`/`default` WOULD close it, and stay forbidden below.
    provider_schema = schemas["Provider"]
    assert set(provider_schema) <= {"type", "description", "examples"}
    assert provider_schema["type"] == "string"
    assert (
        provider_schema["description"]
        == "Infrastructure provider currently hosting the resource."
    )
    assert schemas["RegionPlacement"]["properties"]["provider"]["type"] == "string"

    def check_placement_fields(value: object) -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                if name in {"provider", "region"} and isinstance(child, dict):
                    assert "default" not in child
                    assert "enum" not in child
                check_placement_fields(child)
        elif isinstance(value, list):
            for child in value:
                check_placement_fields(child)

    check_placement_fields(contract)


def test_public_surface_uses_only_current_names_and_copy() -> None:
    contract = json.loads((REPO_ROOT / "openapi.json").read_text())
    schemas = contract["components"]["schemas"]

    assert "network" not in schemas["ForkRequest"]["properties"]
    assert "egress" not in schemas["ForkRequest"]["properties"]
    assert schemas["RunRequest"]["properties"]["background"]["deprecated"] is True
    assert schemas["ForkRequest"]["properties"]["source_org_id"]["deprecated"] is True

    typescript = (REPO_ROOT / "typescript/src/index.ts").read_text()
    python_init = (REPO_ROOT / "python/src/arker/__init__.py").read_text()
    removed_exports = {
        "InboundPortRequest",
        "MintSessionPtyTicketResponse",
        "NetworkPolicy",
        "NetworkPolicyInput",
        "NetworkRequest",
        "NetworkStatus",
        "PutPoliciesResponse",
        "RunStatusResponse",
        "SessionInfo",
    }
    for name in removed_exports:
        assert f"export type {name}" not in typescript
    for name in {"RunStatusResponse", "SessionInfo", "VmInfo", "VmSummary"}:
        assert f'"{name}"' not in python_init

    transition_terms = re.compile(
        r"\b(?:deprecated|formerly|historical|legacy|migration|no longer|previously|retired|today)\b",
        re.IGNORECASE,
    )
    public_sources = (
        Path("python/README.md"),
        Path("python/src/arker/__init__.py"),
        Path("python/src/arker/computer.py"),
        Path("typescript/README.md"),
        Path("cli/src/cli.ts"),
        Path("typescript/src/index.ts"),
    )
    for relative_path in public_sources:
        source = (REPO_ROOT / relative_path).read_text()
        assert transition_terms.search(source) is None, relative_path

    cli = (REPO_ROOT / "cli/src/cli.ts").read_text()
    assert "--time-to-background" in cli
    assert "--background" not in cli


def test_sync_from_local_contract_regenerates_all_managed_files() -> None:
    with tempfile.TemporaryDirectory() as output_directory:
        run(
            "./scripts/sync-openapi",
            "--source-file",
            "openapi.json",
            "--output-root",
            output_directory,
        )

        for relative_path in MANAGED_PATHS:
            assert (Path(output_directory) / relative_path).read_bytes() == (
                REPO_ROOT / relative_path
            ).read_bytes()


def test_sync_requires_an_explicit_local_contract() -> None:
    result = run("./scripts/sync-openapi", check=False)

    assert result.returncode == 2
    assert "--source-file" in result.stderr


def test_check_detects_generated_drift() -> None:
    with tempfile.TemporaryDirectory() as candidate_directory:
        candidate = Path(candidate_directory)
        for relative_path in MANAGED_PATHS:
            destination = candidate / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / relative_path, destination)

        run(
            "./scripts/check-openapi",
            "--candidate-root",
            str(candidate),
        )

        generated_python = candidate / PYTHON_PATH
        generated_python.write_text(generated_python.read_text() + "# drift\n")
        result = run(
            "./scripts/check-openapi",
            "--candidate-root",
            str(candidate),
            check=False,
        )
        assert result.returncode == 1
        assert str(PYTHON_PATH) in result.stderr


def test_contract_tooling_is_repository_local() -> None:
    assert not (REPO_ROOT / ".github/workflows/openapi-freshness.yml").exists()
    assert not (REPO_ROOT / "scripts/check-openapi-freshness").exists()
    assert not (REPO_ROOT / "contract/source.json").exists()

    contract_paths = [
        REPO_ROOT / "scripts/openapi_contract.py",
        REPO_ROOT / "scripts/sync-openapi",
        REPO_ROOT / "scripts/check-openapi",
        REPO_ROOT / "scripts/check-local",
    ]
    checked_source = "\n".join(path.read_text() for path in contract_paths)
    for forbidden in (
        '"gh"',
        "api.github.com",
        "pull_request_target:",
        "secrets.",
    ):
        assert forbidden not in checked_source


def test_pre_push_hook_runs_fast_local_contract_checks() -> None:
    hook = (REPO_ROOT / ".githooks/pre-push").read_text()
    installer = (REPO_ROOT / "scripts/install-hooks").read_text()

    assert "scripts/check-openapi" in hook
    assert "tests/test_openapi_enforcement.py --fast" in hook
    assert "bun run typecheck" in hook
    assert "core.hooksPath .githooks" in installer


def test_full_local_check_covers_all_generated_surfaces() -> None:
    check = (REPO_ROOT / "scripts/check-local").read_text()

    assert "./scripts/check-openapi" in check
    assert "python3 tests/test_openapi_enforcement.py" in check
    assert "python -m pytest" in check
    assert "bun run typecheck" in check
    assert "bun run test" in check


FAST_TESTS = (
    test_public_wire_types_are_generated,
    test_sdk_runtime_uses_only_current_public_error_codes,
    test_retired_vm_pin_is_not_in_the_public_sdk_contract,
    test_provider_and_region_contract_values_are_open_ended,
    test_public_surface_uses_only_current_names_and_copy,
    test_contract_tooling_is_repository_local,
)

ALL_TESTS = (
    test_generation_is_deterministic_for_both_languages,
    *FAST_TESTS,
    test_sync_from_local_contract_regenerates_all_managed_files,
    test_sync_requires_an_explicit_local_contract,
    test_check_detects_generated_drift,
    test_pre_push_hook_runs_fast_local_contract_checks,
    test_full_local_check_covers_all_generated_surfaces,
)


if __name__ == "__main__":
    tests = FAST_TESTS if "--fast" in sys.argv[1:] else ALL_TESTS
    for test in tests:
        test()
    print("PASS openapi enforcement")
