"""Re-export of the CI result schema.

The harness's evidence bundle *is* the CI result bundle -- same files, same
shape -- because the harness proves a Firefox bump safe by running the same
suites a pull request runs. This module exists so harness code and its tests can
keep saying `evidence`, which is what the thing is to them.
"""

from ci.results import (  # noqa: F401  -- re-exported on purpose
    ERROR,
    FAIL,
    PASS,
    SCHEMA,
    SKIP,
    GateResult,
    load,
    load_all,
    run_id,
)
