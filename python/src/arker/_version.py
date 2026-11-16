# Derived from installed package metadata rather than hardcoded: the literal
# drifted from pyproject.toml (reported 0.8.2 while the 0.8.3 distribution was
# live), and a hardcoded copy re-drifts on any release that forgets to bump it.
# pyproject.toml stays the single source of truth.
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("arker")
except PackageNotFoundError:  # source tree, not installed
    __version__ = "0.0.0.dev0"
