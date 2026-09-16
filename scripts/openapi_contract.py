#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CONTRACT_PATH = Path("openapi.json")
TYPESCRIPT_PATH = Path("typescript/src/generated/api-types.ts")
PYTHON_PATH = Path("python/src/arker/generated/api_models.py")
PYTHON_CONTRACT_PATH = Path("python/src/arker/_openapi.json")
MANAGED_PATHS = (CONTRACT_PATH, TYPESCRIPT_PATH, PYTHON_PATH)
REPO_ROOT = Path(__file__).resolve().parents[1]


class ContractError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    cwd: Path = REPO_ROOT,
) -> None:
    try:
        subprocess.run(
            command,
            cwd=cwd,
            check=True,
        )
    except FileNotFoundError as error:
        raise ContractError(f"required command not found: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        raise ContractError(
            f"command failed with exit code {error.returncode}: {' '.join(command)}"
        ) from error


def validate_contract(content: bytes) -> None:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError("source contract is not valid JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("openapi"), str):
        raise ContractError("source contract is not an OpenAPI document")


def generate(contract: Path, output_root: Path) -> None:
    contract = contract.resolve()
    validate_contract(contract.read_bytes())

    typescript_output = output_root / TYPESCRIPT_PATH
    python_output = output_root / PYTHON_PATH
    typescript_output.parent.mkdir(parents=True, exist_ok=True)
    python_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(contract, output_root / PYTHON_CONTRACT_PATH)

    typescript_generator = REPO_ROOT / "typescript/node_modules/.bin/openapi-typescript"
    if not typescript_generator.is_file():
        raise ContractError(
            "TypeScript dependencies are missing; run `bun ci` in typescript/"
        )

    run(
        [
            str(typescript_generator),
            str(contract),
            "--default-non-nullable",
            "false",
            "-o",
            str(typescript_output),
        ],
    )
    run(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT / "python"),
            "datamodel-codegen",
            "--input",
            str(contract),
            "--input-file-type",
            "openapi",
            "--output",
            str(python_output),
            "--output-model-type",
            "dataclasses.dataclass",
            "--target-python-version",
            "3.10",
            "--openapi-scopes",
            "schemas",
            "paths",
            "parameters",
            "--use-operation-id-as-name",
            "--use-standard-collections",
            "--use-union-operator",
            "--use-subclass-enum",
            "--enum-field-as-literal",
            "all",
            "--enum-field-as-literal-map",
            '{"Vgpu":"enum"}',
            "--formatters",
            "black",
            "isort",
            "--frozen-dataclasses",
            "--disable-timestamp",
            "--include-path-parameters",
        ],
    )


def stage_contract(output_root: Path, contract: bytes) -> None:
    contract_output = output_root / CONTRACT_PATH
    contract_output.parent.mkdir(parents=True, exist_ok=True)
    contract_output.write_bytes(contract)
    generate(contract_output, output_root)


def copy_managed_files(source_root: Path, output_root: Path) -> None:
    for relative_path in (*MANAGED_PATHS, PYTHON_CONTRACT_PATH):
        source = source_root / relative_path
        destination = output_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)


def load_vendored_source() -> bytes:
    try:
        contract = (REPO_ROOT / CONTRACT_PATH).read_bytes()
    except FileNotFoundError as error:
        raise ContractError(f"vendored contract is missing: {CONTRACT_PATH}") from error
    validate_contract(contract)
    return contract


def local_candidate(root: Path) -> dict[Path, bytes]:
    candidate: dict[Path, bytes] = {}
    for relative_path in MANAGED_PATHS:
        path = root / relative_path
        candidate[relative_path] = path.read_bytes() if path.is_file() else b""
    return candidate


def print_diff(relative_path: Path, expected: bytes, actual: bytes) -> None:
    try:
        expected_text = expected.decode().splitlines(keepends=True)
        actual_text = actual.decode().splitlines(keepends=True)
    except UnicodeDecodeError:
        print(f"drift detected: {relative_path} differs", file=sys.stderr)
        return

    print(f"drift detected: {relative_path}", file=sys.stderr)
    diff = difflib.unified_diff(
        actual_text,
        expected_text,
        fromfile=f"candidate/{relative_path}",
        tofile=f"expected/{relative_path}",
    )
    sys.stderr.writelines(diff)


def command_generate(args: argparse.Namespace) -> int:
    generate(Path(args.contract), Path(args.output_root).resolve())
    return 0


def command_sync(args: argparse.Namespace) -> int:
    source = Path(args.source_file).resolve()
    try:
        contract = source.read_bytes()
    except FileNotFoundError as error:
        raise ContractError(f"source contract not found: {source}") from error
    validate_contract(contract)

    output_root = Path(args.output_root).resolve()
    with tempfile.TemporaryDirectory(prefix="arker-openapi-sync-") as directory:
        stage = Path(directory)
        stage_contract(stage, contract)
        copy_managed_files(stage, output_root)

    print(f"synced {CONTRACT_PATH} from {source}")
    return 0


def command_check(args: argparse.Namespace) -> int:
    contract = load_vendored_source()

    with tempfile.TemporaryDirectory(prefix="arker-openapi-check-") as directory:
        expected_root = Path(directory)
        stage_contract(expected_root, contract)
        expected = {path: (expected_root / path).read_bytes() for path in MANAGED_PATHS}

    actual = local_candidate(Path(args.candidate_root or REPO_ROOT).resolve())

    drift = False
    for relative_path in MANAGED_PATHS:
        if actual[relative_path] != expected[relative_path]:
            print_diff(relative_path, expected[relative_path], actual[relative_path])
            drift = True
    if drift:
        return 1

    print("generated artifacts match openapi.json")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Synchronize and verify the public OpenAPI contract"
    )
    subcommands = root.add_subparsers(dest="command", required=True)

    generate_parser = subcommands.add_parser("generate")
    generate_parser.add_argument("--contract", required=True)
    generate_parser.add_argument("--output-root", default=str(REPO_ROOT))
    generate_parser.set_defaults(handler=command_generate)

    sync_parser = subcommands.add_parser("sync")
    sync_parser.add_argument("--source-file", required=True)
    sync_parser.add_argument("--output-root", default=str(REPO_ROOT))
    sync_parser.set_defaults(handler=command_sync)

    check_parser = subcommands.add_parser("check")
    check_parser.add_argument("--candidate-root")
    check_parser.set_defaults(handler=command_check)

    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
