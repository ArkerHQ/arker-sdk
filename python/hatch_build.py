from pathlib import Path
from shutil import copyfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        contract = root / "openapi.json"
        if not contract.exists():
            contract = root.parent / "openapi.json"
        copyfile(contract, root / "src/arker/_openapi.json")
        target = "openapi.json" if self.target_name == "sdist" else "arker/_openapi.json"
        build_data["force_include"][str(contract)] = target
