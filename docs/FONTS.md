# Bundled fonts: what ships, what is reported, how to verify

Camoufox ships one font bundle per spoofed OS (`bundle/fonts/{linux,windows,macos}`)
and a fontconfig per OS (`bundle/fontconfig/<os>/fonts.conf`). The font model in
`pythonlib/camoufox` (`fonts.json`, the `_ESSENTIAL_FONTS_*` / `_BASE_VARIANT_FONTS_*`
/ `_*_MARKER_FONTS` constants, `font-groups.json`) and the fontconfig files are
generated against a larger bundle than the one currently in git:

|        | current git bundle (files / families) | target bundle (files / families) | size  | reportable (`fonts.json`) |
|--------|----------------------------------------|----------------------------------|-------|---------------------------|
| linux  | 143 / 134                              | 863 / 600                        | 1.0 GB | 342 (was 134) |
| windows| 144 / 106                              | 1074 / 541                       | 1.3 GB | 339 (was 107) |
| macos  | 355 / 508                              | 1127 / 888                       | 1.8 GB | 610 (was 574) |

The target bundle is ~3.9 GB and contains a file over GitHub's 100 MB limit
(`Apple Color Emoji.ttc`, 183 MiB), so it is delivered separately from the code;
until it is in place, `scripts/verify-fonts.py` reports the names the current
bundle cannot render.

"Families" = what `fc-scan` publishes for the dir plus the scan-time, alias and
browser-shipped names below; "reportable" = the per-OS list in
`pythonlib/camoufox/fonts.json`.

## The invariant

Every family the launcher may **report** for an OS must be one the packaged
browser can **render** under that OS's bundled fontconfig. The report side is
`fonts.json[os]` plus the constants in `pythonlib/camoufox/fingerprints.py`
(`_ESSENTIAL_FONTS_*`, `_BASE_VARIANT_FONTS_*`, `_*_MARKER_FONTS`); the render
side is what `fc-list` publishes under a runtime copy of `fonts.conf` (which
`utils._generate_fontconfig` produces by rewriting `<dir prefix="cwd">fonts</dir>`
to the absolute package `fonts/` dir). The font allowlist patch
(`patches/font-hijacker.patch`) prunes lookups by family name AFTER fontconfig
substitution, so a reported name that fontconfig cannot resolve measures as the
fallback: a reverse leak. The old `fonts.json[mac]` carried 70 such names
(Hiragino etc.); the new lists carry none.

The reverse direction is deliberately NOT tight: the bundle renders more
families than are ever reported (202 / 278 / 277 per OS, listed by
`scripts/gen-fonts-json.py --dump-union`). Those are names a real OS never
presents as a family: weight-variant subfamilies DirectWrite folds into their
parent ("Barlow Black"), the bare "Sitka" / "Segoe UI Variable" umbrella names
(Windows presents only their optical-size instances), and CJK families a stock
Windows install does not report. They stay renderable but unreported.

## How `fonts.json` is derived (`scripts/gen-fonts-json.py`)

```
reportable[os] = fc-scan(bundle/fonts/<os>)              families fontconfig publishes
               + SCAN_FAMILIES                           Sitka * / Segoe UI Variable * (windows conf <match target="scan">)
               + ALIASES                                 names fonts.conf rewrites to a bundled target (below)
               + SHIPPED_BY_BROWSER                      Twemoji Mozilla (dist/bin/fonts, from the Firefox build)
               ∩ names(scripts/data/font-manifests.json) bases + additions + MARKER_FONTS for that OS
               + ALIASES, REPORTABLE_EXTRA               forced in (see below)
```

`scripts/data/font-manifests.json` is the per-OS model: the base font set of
each OS version with its real-world share (Windows 10 / 11, macOS Sonoma,
Ubuntu / Mint) and the optional additions (Office, Pan-European, LibreOffice,
Adobe CC, developer and web fonts) with their install probability.
`--print-bases` prints the OS base lists intersected with the result, which is
what the `_ESSENTIAL_FONTS_*` constants are pasted from. Regenerate both
together whenever the bundle changes; the committed `fonts.json` is exactly what
the script produces from the target bundle.

Deliberate choices:

- **Windows aliases are unconditional and always reported.** The 13
  GDI-substitution / Light-Semilight rules (`Courier -> Courier New`,
  `Helvetica -> Arial`, `Calibri Light -> Calibri`, ...) are in
  `bundle/fontconfig/windows/fonts.conf` unconditionally, because Camoufox has no
  per-launch fontconfig hook, and the names are in `_ESSENTIAL_FONTS_WINDOWS`.
  That matches reality: every Windows install resolves all of them.
- **macOS TTC weight names are reported.** The confs rewrite `American
  Typewriter Semibold`, `Futura Bold`, `STIX Two Math Regular`, ... to their base
  family. A real Mac registers those names as families (they are in the macOS
  base), but `fc-scan` cannot see them; here the 8 whose target is bundled are
  reportable and essential. The three `PingFang * Light` rewrites are inert
  (PingFang is not bundled).
- **`Twemoji Mozilla` is reportable on Linux** as before (it is a CreepJS
  Linux marker and Firefox exposes its bundled font to content). It is not
  reported on macOS.
- The same rewrite block leaves one live-but-unreported name on Linux
  (`Noto Sans Canadian Aboriginal Regular`); a real Ubuntu does not report it,
  so it stays unreported and `verify-fonts.py` warns rather than fails.

## The per-launch draw (`fingerprints._generate_random_font_subset`)

Unchanged in shape (same name and signature, same 30-78% draw), re-based on a
model of what varies between real machines:

1. **Essential = the OS base**, always present in full: a real machine has all
   of its OS defaults. `_ESSENTIAL_FONTS_WINDOWS` = the Windows 10 base with the
   stock CJK families = 119 names; `_ESSENTIAL_FONTS_MACOS` = the Sonoma base ∩
   renderable + the 8 alias names = 550; `_ESSENTIAL_FONTS_LINUX` = the
   Ubuntu/Mint common core = 234.
2. **OS-version variant, all-or-nothing** (`_BASE_VARIANT_FONTS_*`): the seven
   Windows 11 additions with p = 0.65, `Liberation Sans Narrow` (Ubuntu, not
   Mint) with p = 0.65, nothing on macOS.
3. **A random 30-78 % of the additions** (`fonts.json` minus base and variant —
   Office, LibreOffice, Adobe CC, Cascadia, Meslo, developer and web fonts),
   drawn as co-shipped groups (`font-groups.json`) so a family set that installs
   together is never split.
4. **Marker fonts** appended if missing (`_ensure_marker_fonts`), minus the
   three PingFang names on macOS, which the bundle cannot render.

For a **native** identity (the host's own OS) on macOS and Windows the draw
claims only the OS base: the real system fonts are the right ones there, and
`font-hijacker.patch` does not activate the bundle for them.

## fontconfig parity

Linux: generics resolve to what a stock Ubuntu control does (Noto Sans / Noto
Serif / DejaVu Sans Mono / Z003), `hintstyle` is `hintslight`, the stock
metric aliases (30-metric-aliases), 49-sansserif, urw-base35 and the non-Latin
rule files (40-nonlatin / 60-latin / 65-* / 70-fonts-noto-cjk) are merged in
stock conf.d order. Windows: adds the `MS Shell Dlg 2 -> Tahoma` dialog family,
cursive/fantasy generics, the 243 duplicate-face rejects, the `Sitka` /
`Segoe UI Variable` scan-time instance families, plus the alias block above.
Every reject glob is listed twice, `*/fonts/windows/<file>` for the Linux
package layout and `*/fonts/<file>` for the flattened macOS/Windows packages
(`scripts/package.py` flattens non-Linux; no basename collides across the dirs
that share a package). The `<dir prefix="cwd">fonts</dir>` line that `utils.py`
rewrites is kept in all three.

## Verifying

```
python3 scripts/verify-fonts.py            # full: builds a fontconfig cache over the bundle, minutes on first run
python3 scripts/verify-fonts.py --quick    # constants + draws + layout only
cd pythonlib && python3 -m pytest -q -k "font or humanize or launch_environment"
```

The full run checks, per OS: `fonts.json[os]` ⊆ `fc-list` families (aliases via
`fc-match`), generics resolve to a reportable family, essential / variant /
marker ⊆ `fonts.json`, live alias rewrites are essential, 20 draws stay inside
`fonts.json`, no subfolders or cross-dir basename collisions, reject globs name
real files, and reports files over GitHub's 100 MB limit.

## Known residue

- The Linux package copies all three OS dirs (`--fonts windows macos linux`)
  without de-duplicating identical files across them, so it is ~2x larger than
  necessary; `scripts/package.py` was not changed.
- Six reject globs contain `[wdth,wght]`-style brackets that fontconfig reads as
  character classes.
- `PingFang.ttc` and `holomdl2.ttf` from the old bundle are not in the target
  bundle; they were dropped and are recoverable from git history if needed.
