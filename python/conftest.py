"""
Make the in-repo sources importable as `camoufox` without an install.

The package sources live in `src/` (mirroring `typescript/src/`), but the
import name — in the tests, in the docs and in the built wheel — is
`camoufox`. `pyproject.toml` does that mapping at build time
(`packages = [{ include = "*", from = "src", to = "camoufox" }]`); this does
the same for a plain `python -m pytest` against the working tree.

Binding `__path__` to `src/` is what keeps the package's own relative
imports (`from .pkgman import ...`) resolving, which a bare
`sys.path.insert(0, "src")` would not do — that would import the modules as
unrelated top-level names.
"""

import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).parent / "src"

if "camoufox" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "camoufox",
        SRC / "__init__.py",
        submodule_search_locations=[str(SRC)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - layout guard
        raise ImportError(f"cannot load the camoufox package from {SRC}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec: `camoufox/__init__.py` imports its own submodules,
    # and those do `from camoufox... import`-style resolution through
    # sys.modules while the parent is still initialising.
    sys.modules["camoufox"] = module
    spec.loader.exec_module(module)
