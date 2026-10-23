#!/usr/bin/env python3
"""Profile-guided optimization for release builds, the way stock Firefox does it.

Stock builds every desktop release in three stages (Firefox 158's taskcluster
kinds instrumented-build, generate-profile and build with `use-pgo`):

  1. an instrumented build, cross-compiled on Linux
     (CAMOUFOX_PGO_GENERATE=1: --enable-profile-generate=cross);
  2. a training run of that build, packaged as it ships, on the target OS:
     build/pgo/profileserver.py over the in-tree pages, Speedometer3 and
     JetStream3, merged by llvm-profdata, with the omni.ja order it read;
  3. the release build with that profile (CAMOUFOX_PGO_PROFILE=<dir>:
     --enable-profile-use=cross, LTO as stock picks it; see base.mozconfig).

A profile only fits the code it was trained on, so each one records the
native-input digest of the tree that built it and `use` refuses any other.

Run:
    python3 -m ci.pgo train --binary <packaged instrumented camoufox-bin> --platform linux-x86_64 --out D
    python3 -m ci.pgo use --profile D --target linux-arm64
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import List, Optional

from ._pytest import source_dir
from ._util import die, log, read_upstream_sh, run, summary
from .browser_inputs import native_digest

PROFDATA = "merged.profdata"
JARLOG = "en-US.log"
MANIFEST = "profile.json"

# The profile each release target is built with, as `use-pgo` assigns them in
# Firefox 158's taskcluster/kinds/build: Linux arm64 uses the x86_64 Linux one.
PROFILE_FOR = {
    "linux-x86_64": "linux-x86_64",
    "linux-arm64": "linux-x86_64",
    "windows-x86_64": "windows-x86_64",
    "windows-i686": "windows-i686",
    "macos-x86_64": "macos-x86_64",
    "macos-arm64": "macos-arm64",
}

# camoufox.cfg ends the process at fastShutdownStage 3, before child processes
# have written their profiles. Stock instrumented builds run at 0
# (StaticPrefList.yaml, MOZ_PROFILE_GENERATE); a user pref beats the default.
FLUSH_PREF = 'user_pref("toolkit.shutdown.fastShutdownStage", 0);'


def extended_corpus(src: Path, dest: Path) -> Path:
    """JetStream3, from the URL and sha256 the tree's pgo-extended-corpus fetch pins."""
    fetches = (src / "taskcluster" / "kinds" / "fetch" / "benchmarks.yml").read_text(encoding="utf-8")
    entry = re.search(r"^pgo-extended-corpus:\n((?:[ \t]+.*\n)+)", fetches, re.MULTILINE)
    if not entry:
        die("taskcluster/kinds/fetch/benchmarks.yml has no pgo-extended-corpus fetch")
    url = re.search(r"^\s+url: (\S+)$", entry.group(1), re.MULTILINE).group(1)
    sha = re.search(r"^\s+sha256: ([0-9a-f]{64})$", entry.group(1), re.MULTILINE).group(1)
    archive = dest / "jetstream.tar.gz"
    log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=900) as resp, open(archive, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != sha:
        die(f"{url}: sha256 {actual}, the tree pins {sha}")
    corpus = dest / "pgo-extended-corpus"
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            # Mozilla's fetch: strip-components 1, add-prefix JetStream/.
            parts = Path(member.name).parts[1:]
            if not parts:
                continue
            member.name = str(Path("JetStream", *parts))
            tar.extract(member, corpus, filter="data")
    archive.unlink()
    return corpus


def cmd_train(args) -> int:
    src = Path(args.src) if args.src else source_dir()
    binary = Path(args.binary).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    llvm_profdata = Path(os.environ.get("MOZBUILD_STATE_PATH", Path.home() / ".mozbuild")) / "clang" / "bin" / (
        "llvm-profdata.exe" if os.name == "nt" else "llvm-profdata")
    if not binary.is_file():
        die(f"no instrumented build at {binary}")
    if not llvm_profdata.is_file():
        die(f"no llvm-profdata at {llvm_profdata}: run mach bootstrap")

    user_js = src / "testing" / "profiles" / "profileserver" / "user.js"
    text = user_js.read_text(encoding="utf-8")
    if FLUSH_PREF not in text:
        user_js.write_text(text.rstrip("\n") + "\n" + FLUSH_PREF + "\n", encoding="utf-8")
    corpus = extended_corpus(src, out)

    env = {
        "LLVM_PROFDATA": str(llvm_profdata),
        "JARLOG_FILE": JARLOG,
        # profileserver.py writes its run logs here and fails on any
        # "LLVM Profile Error" in them, as it does in automation.
        "UPLOAD_PATH": str(out),
        # With UPLOAD_PATH set it also looks for minidumps with a stackwalker
        # from here. Camoufox has no crash reporter, so there are none to walk.
        "MOZ_FETCHES_DIR": str(out),
    }
    for stale in src.glob("*.profraw"):
        stale.unlink()
    res = run([sys.executable, "mach", "python", "build/pgo/profileserver.py",
               "--binary", str(binary), "--extended-corpus", str(corpus)],
              cwd=src, env=env, timeout=args.timeout, tee=True, capture=False)
    shutil.rmtree(corpus)
    if res.code != 0:
        die(f"profileserver.py exited {res.code}; its logs are in {out}")
    for name in (PROFDATA, JARLOG):
        made = src / name
        if not made.is_file() or made.stat().st_size == 0:
            die(f"training produced no {name}")
        shutil.move(str(made), out / name)
    for raw in src.glob("*.profraw"):
        raw.unlink()
    (out / MANIFEST).write_text(json.dumps({
        "firefox": read_upstream_sh()["version"],
        "platform": args.platform,
        "native_digest": native_digest(),
    }, indent=2) + "\n", encoding="utf-8")
    log(f"{out / PROFDATA}: {(out / PROFDATA).stat().st_size >> 20} MB")
    return 0


def cmd_use(args) -> int:
    profile = Path(args.profile).resolve()
    want = PROFILE_FOR.get(args.target) or die(f"no profile is assigned to {args.target}")
    try:
        manifest = json.loads((profile / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"{profile} holds no profile: a release build is never made without one")
    digest = native_digest()
    if manifest.get("platform") != want:
        die(f"{args.target} builds with the {want} profile, {profile} is {manifest.get('platform')}")
    if manifest.get("native_digest") != digest:
        die(f"the profile was trained on native inputs {manifest.get('native_digest')}, this tree is {digest}")
    for name in (PROFDATA, JARLOG):
        if not (profile / name).is_file():
            die(f"{profile} has no {name}")
    path = os.environ.get("GITHUB_ENV")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"CAMOUFOX_PGO_PROFILE={profile}\n")
    log(f"CAMOUFOX_PGO_PROFILE={profile}")
    summary(f"{args.target}: PGO profile `{want}`, trained on native inputs `{digest}`")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    train = sub.add_parser("train")
    train.add_argument("--binary", required=True)
    train.add_argument("--platform", required=True, choices=sorted(set(PROFILE_FOR.values())))
    train.add_argument("--out", required=True)
    train.add_argument("--src", help="the Firefox source tree (default: this checkout's)")
    train.add_argument("--timeout", type=int, default=5400)
    train.set_defaults(fn=cmd_train)
    use = sub.add_parser("use")
    use.add_argument("--profile", required=True)
    use.add_argument("--target", required=True, choices=sorted(PROFILE_FOR))
    use.set_defaults(fn=cmd_use)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
