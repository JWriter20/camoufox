#!/usr/bin/env python3

"""
The script that patches the Firefox source into the Camoufox source.
Based on LibreWolf's patch script:
https://gitlab.com/librewolf-community/browser/source/-/blob/main/scripts/librewolf-patches.py

Run:
    python3 scripts/init-patch.py <version> <release>
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from _mixin import (
    find_src_dir,
    get_moz_target,
    get_options,
    list_patches,
    patch,
    run,
    temp_cd,
)

options, args = get_options()

"""
Main patcher functions
"""


@dataclass
class Patcher:
    """Patch and prepare the Camoufox source"""

    moz_target: str
    target: str

    def camoufox_patches(self):
        """
        Apply all patches
        """
        version, release = extract_args()
        with temp_cd(find_src_dir('.', version, release)):
            # Reset to unpatched state first (like "Find broken patches")
            print("Resetting to unpatched state...")
            run('git clean -fdx && ./mach clobber && git reset --hard unpatched', exit_on_fail=False)

            # Re-copy additions and settings after reset
            print("Re-copying additions and settings...")
            run(f'bash ../scripts/copy-additions.sh {version} {release}')

            # Create the base mozconfig file
            run('cp -v ../assets/base.mozconfig mozconfig')
            # Set cross building target
            print(f'Using target: {self.moz_target}')
            self._update_mozconfig()

            if not options.mozconfig_only:
                # Apply patches with roverfox patches at the very end
                all_patches = list_patches()
                # Normalize paths and partition into non-roverfox and roverfox
                non_roverfox = []
                roverfox = []
                for p in all_patches:
                    norm = os.path.normpath(p)
                    parts = norm.split(os.sep)
                    if 'roverfox' in parts:
                        roverfox.append(p)
                    else:
                        non_roverfox.append(p)

                # Track patch failures
                failed_patches = []

                # Apply non-roverfox patches first
                for patch_file in non_roverfox:
                    rejects = self._apply_and_check(patch_file)
                    if rejects:
                        failed_patches.append((patch_file, rejects))

                # Apply roverfox patches last
                for patch_file in roverfox:
                    rejects = self._apply_and_check(patch_file)
                    if rejects:
                        failed_patches.append((patch_file, rejects))

                # Report failures
                if failed_patches:
                    print('\n' + '='*70)
                    print(f'ERROR: {len(failed_patches)} patch(es) failed to apply cleanly:')
                    print('='*70)
                    for patch_file, rejects in failed_patches:
                        print(f'\n{patch_file}:')
                        for reject in rejects:
                            print(f'  - {reject}')
                    print('='*70)
                    sys.exit(1)

                # Semantic verification: a clean apply (no .rej) does NOT mean
                # hunks landed where intended. Fuzzy context matching can apply
                # a hunk to the wrong function and still exit 0. These assertions
                # catch that. See scripts/patch-assertions.txt for the why.
                self._verify_assertions()

            print('Complete!')

    def _apply_and_check(self, patch_file):
        """
        Apply a patch and check for reject files.
        Returns list of reject files if any, empty list otherwise.
        """
        import time

        print(f"\n*** -> patch -p1 -i {patch_file}")
        sys.stdout.flush()

        # Record time before applying so we only detect .rej files from this patch
        start_time = time.time()

        # Apply patch interactively - don't capture stdout/stderr at all
        # This allows prompts to show immediately and user can respond
        # --forward flag: skip patches that appear to be already applied
        # --binary flag: preserve line endings (helps with CRLF vs LF differences)
        # -l flag: ignore whitespace differences
        result = subprocess.run(
            ['patch', '-p1', '--forward', '-l', '--binary', '-i', patch_file],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True
        )

        # After patch completes, search for any .rej files created during this patch
        rejects = []
        for root, dirs, files in os.walk('.'):
            for file in files:
                if file.endswith('.rej'):
                    reject_path = os.path.join(root, file)
                    if os.path.exists(reject_path):
                        # Only include if created after this patch started
                        if os.path.getmtime(reject_path) >= start_time:
                            rejects.append(reject_path)

        # Clean up .rej files so they don't interfere with subsequent patches
        for rej in rejects:
            os.remove(rej)

        return rejects

    def _verify_assertions(self):
        """
        Run scripts/patch-assertions.txt against the patched source tree.

        A patch that applies with no .rej can still have landed in the wrong
        place (fuzzy context match), or be a silent no-op. These assertions
        verify the *semantic* result — required strings present, in the right
        function, forbidden strings absent — and abort the build if any fail.
        """
        manifest = os.path.join('..', 'scripts', 'patch-assertions.txt')
        if not os.path.exists(manifest):
            print('WARNING: scripts/patch-assertions.txt missing — skipping '
                  'post-patch verification')
            return

        print('\n' + '=' * 70)
        print('Verifying post-patch assertions...')
        print('=' * 70)

        # Match a top-level function-opening line like:
        #   nsresult HTMLInputElement::InitFilePicker(FilePickerType aType) {
        # Used to bound an in_fn / absent_in_fn region to a single function.
        fn_open = re.compile(r'^\s*[\w:<>,&*\s]+::\w+\s*\(')

        def read_file(rel):
            path = rel
            if not os.path.exists(path):
                return None
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                return fh.read()

        def function_body(text, sig_substr):
            """Return the slice of `text` from the line containing sig_substr up
            to (but not including) the next top-level function-opening line."""
            lines = text.split('\n')
            start = None
            for i, ln in enumerate(lines):
                if sig_substr in ln:
                    start = i
                    break
            if start is None:
                return None
            end = len(lines)
            for j in range(start + 1, len(lines)):
                if fn_open.match(lines[j]) and sig_substr not in lines[j]:
                    end = j
                    break
            return '\n'.join(lines[start:end])

        failures = []
        checked = 0
        with open(manifest, 'r', encoding='utf-8') as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                parts = [p.strip() for p in line.split('|||')]
                if len(parts) < 3:
                    failures.append(f'{manifest}:{lineno}: malformed assertion '
                                    f'(need at least: directive ||| file ||| '
                                    f'arg): {line}')
                    continue
                directive = parts[0]
                rel = parts[1]
                # Shift so parts[1]/parts[2] below mean arg1/arg2 as before.
                parts = [directive] + parts[2:]
                checked += 1

                text = read_file(rel)
                if text is None:
                    failures.append(f'[{directive}] {rel}: FILE NOT FOUND')
                    continue

                if directive == 'contains':
                    needle = parts[1]
                    if needle not in text:
                        failures.append(
                            f'[contains] {rel}: missing expected string:\n'
                            f'           {needle!r}')
                elif directive == 'absent':
                    needle = parts[1]
                    if needle in text:
                        failures.append(
                            f'[absent] {rel}: forbidden string present:\n'
                            f'           {needle!r}')
                elif directive in ('in_fn', 'absent_in_fn') and len(parts) < 3:
                    failures.append(
                        f'{manifest}:{lineno}: {directive} needs '
                        f'4 fields (directive ||| file ||| fn ||| substring)')
                elif directive == 'in_fn':
                    sig, needle = parts[1], parts[2]
                    body = function_body(text, sig)
                    if body is None:
                        failures.append(
                            f'[in_fn] {rel}: function matching {sig!r} not found')
                    elif needle not in body:
                        failures.append(
                            f'[in_fn] {rel}: {needle!r}\n'
                            f'        NOT found inside function {sig!r} '
                            f'(landed elsewhere or missing)')
                elif directive == 'absent_in_fn':
                    sig, needle = parts[1], parts[2]
                    body = function_body(text, sig)
                    if body is None:
                        # Function gone entirely — can't violate; warn only.
                        print(f'  note: [absent_in_fn] function {sig!r} not '
                              f'found in {rel}; skipping')
                    elif needle in body:
                        failures.append(
                            f'[absent_in_fn] {rel}: {needle!r}\n'
                            f'        WRONGLY present inside function {sig!r}')
                else:
                    failures.append(
                        f'{manifest}:{lineno}: unknown directive {directive!r}')

        if failures:
            print('\n' + '=' * 70)
            print(f'ERROR: {len(failures)} post-patch assertion(s) FAILED '
                  f'({checked} checked):')
            print('=' * 70)
            for f in failures:
                print(f'  - {f}')
            print('=' * 70)
            print('A patch applied without a .rej but produced the wrong '
                  'result. Fix the patch (often: add unique context lines so '
                  'the hunk cannot fuzzy-match the wrong location), then '
                  're-run.')
            sys.exit(1)

        print(f'All {checked} post-patch assertion(s) passed.')

    def _update_mozconfig(self):
        """
        Helper for adding additional mozconfig code from assets/<target>.mozconfig
        """
        mozconfig_backup = "mozconfig.backup"
        mozconfig = "mozconfig"
        mozconfig_hash = "mozconfig.hash"

        # Create backup if it doesn't exist
        if not os.path.exists(mozconfig_backup):
            if os.path.exists(mozconfig):
                shutil.copy2(mozconfig, mozconfig_backup)
            else:
                with open(mozconfig_backup, 'w', encoding='utf-8') as f:
                    pass

        # Read backup content
        with open(mozconfig_backup, 'r', encoding='utf-8') as f:
            content = f.read()

        # Add target option
        content += f"\nac_add_options --target={self.moz_target}\n"

        # Add target-specific mozconfig if it exists
        target_mozconfig = os.path.join("..", "assets", f"{self.target}.mozconfig")
        if os.path.exists(target_mozconfig):
            with open(target_mozconfig, 'r', encoding='utf-8') as f:
                content += f.read()

        # Calculate new hash
        new_hash = hashlib.sha256(content.encode()).hexdigest()

        # Update mozconfig
        print(f"-> Updating mozconfig, target is {self.moz_target}")
        with open(mozconfig, 'w', encoding='utf-8') as f:
            f.write(content)
        with open(mozconfig_hash, 'w', encoding='utf-8') as f:
            f.write(new_hash)


def add_rustup(*targets):
    """Add rust targets"""
    for rust_target in targets:
        run(f'~/.cargo/bin/rustup target add "{rust_target}"')


def _update_rustup(target):
    """Add rust targets for the given target"""
    if target == "linux":
        add_rustup("aarch64-unknown-linux-gnu", "i686-unknown-linux-gnu")
    elif target == "windows":
        add_rustup("x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc", "i686-pc-windows-msvc")
    elif target == "macos":
        add_rustup("x86_64-apple-darwin", "aarch64-apple-darwin")


"""
Preparation
"""


def extract_args():
    """Get version and release from args"""
    if len(args) != 2:
        sys.stderr.write('error: please specify version and release of camoufox source')
        sys.exit(1)
    return args[0], args[1]


AVAILABLE_TARGETS = ["linux", "windows", "macos"]
AVAILABLE_ARCHS = ["x86_64", "arm64", "i686"]


def extract_build_target():
    """Get moz_target if passed to BUILD_TARGET environment variable"""

    if os.environ.get('BUILD_TARGET'):
        target, arch = os.environ['BUILD_TARGET'].split(',')
        assert target in AVAILABLE_TARGETS, f"Unsupported target: {target}"
        assert arch in AVAILABLE_ARCHS, f"Unsupported architecture: {arch}"
    else:
        target, arch = "macos", "arm64"
    return target, arch


"""
Launcher
"""

if __name__ == "__main__":
    # Extract args
    VERSION, RELEASE = extract_args()

    TARGET, ARCH = extract_build_target()
    MOZ_TARGET = get_moz_target(TARGET, ARCH)
    _update_rustup(TARGET)

    # Check if the folder exists
    if not os.path.exists(f'camoufox-{VERSION}-{RELEASE}/configure.py'):
        sys.stderr.write('error: folder doesn\'t look like a Firefox folder.')
        sys.exit(1)

    # Apply the patches
    patcher = Patcher(MOZ_TARGET, TARGET)
    patcher.camoufox_patches()

    sys.exit(0)  # ensure 0 exit code
