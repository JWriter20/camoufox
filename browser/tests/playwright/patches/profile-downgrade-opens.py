"""
Verify a profile last run by a newer Camoufox still opens (daijro/camoufox#994).

Firefox refuses a profile whose compatibility.ini names a newer major version,
and on Windows says so in a modal message box that blocks the launch until
someone clicks it. A persistent context outlives a version switch (an upgrade
rolled back, a `camoufox set` build tried and reverted), so Camoufox builds
without MOZ_BLOCK_PROFILE_DOWNGRADE
(patches/librewolf/disable-data-reporting-at-compile-time.patch).

    opens      a persistent context on that profile loads a page
    stamped    the browser rewrote compatibility.ini with its own version, so
               it ran on that profile rather than a fresh one

    python browser/tests/playwright/patches/profile-downgrade-opens.py
"""

import configparser
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import launch_kwargs, resolve_binary  # noqa: E402


def app_version(binary: Path) -> str:
    ini = configparser.ConfigParser()
    ini.read(binary.parent / "application.ini")
    return ini["App"]["Version"]


def last_version(profile: Path) -> str:
    ini = configparser.ConfigParser()
    ini.read(profile / "compatibility.ini")
    return ini["Compatibility"]["LastVersion"]


def main() -> int:
    from camoufox.sync_api import Camoufox

    version = app_version(resolve_binary())
    newer = f"{int(version.split('.')[0]) + 1}.0"
    profile = Path(tempfile.mkdtemp())
    (profile / "compatibility.ini").write_text(
        f"[Compatibility]\nLastVersion={newer}_20990101000000/20990101000000\n")
    # The override would skip the check this guard exists to exercise.
    env = {k: v for k, v in os.environ.items() if k != "MOZ_ALLOW_DOWNGRADE"}

    failures = []
    try:
        with Camoufox(**launch_kwargs(persistent_context=True, user_data_dir=str(profile),
                                      env=env, i_know_what_im_doing=True)) as context:
            page = context.new_page()
            page.set_content("<p>ok</p>")
            if page.text_content("p") != "ok":
                failures.append("the page did not render")
    except Exception as exc:
        failures.append(f"a profile last run by {newer} did not open: "
                        f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
    stamped = last_version(profile)
    shutil.rmtree(profile, ignore_errors=True)

    print(f"browser {version}, profile last run by {newer}, now stamped {stamped.split('_')[0]}")
    print()
    if not failures and not stamped.startswith(f"{version}_"):
        failures.append(f"compatibility.ini still says {stamped}: the browser did not run on this profile")
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: a profile last run by a newer version opens.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
