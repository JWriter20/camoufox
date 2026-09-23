DO NOT MODIFY THE CONTENTS OF THIS DIRECTORY

Any adjustment to bundled fonts will result in an altered fingerprint. Font
fingerprinting is more than just detecting what fonts you have, it also includes
font fallbacks and characters (unicode code points) and any change in those can
be measured.


WHAT IS HERE

linux/, windows/ and macos/ are byte-for-byte the font bundle of Holo
(browser/bundle/fonts/{linux,windows,macos}, restored from the holo-font-cache
tars fonts-{linux,macos,windows}.tar), adopted on 2026-09-14 in place of the
older 143 / 144 / 355-file Camoufox set:

    linux/     846 files   (~0.9 GB)   577 families
    windows/  1022 files   (~1.3 GB)   520 families
    macos/    1069 files   (~1.8 GB)   878 families

Every OS dir is FLAT (no subfolders): scripts/package.py keeps the per-OS
subdirs on the Linux package but flattens them into fonts/ on the macOS and
Windows packages, and no basename collides across the dirs that share a
package (scripts/verify-fonts.py checks). File names carry the NN__ prefixes
Holo's merge-drop.sh gives fonts as they are unioned in; the prefix is part of
the name the reject globs in ../fontconfig/windows/fonts.conf refer to, so do
not rename files.

Roughly half the content is duplicated across the three dirs (1564 unique
contents in 3064 files): a family a build spoofs for several OSes is bundled
under each. Any fontconfig lookup may therefore pick a same-named face from a
sibling dir; this mirrors Holo's layout.


SOURCES

- Linux: the Tor Browser bundle set plus Ubuntu / Mint desktop defaults
  (Noto, DejaVu, Ubuntu, Liberation, URW base35, ...) and the open additions
  (LibreOffice, developer and web fonts) Holo's fonts-manifests.ts draws from.
- Windows: Windows 11 22H2 / Windows 10 default families, the Office and
  Pan-European FOD sets, LibreOffice, and open developer/web fonts.
  The Windows CJK Feature-on-Demand fonts and third-party Adobe/Kozuka CJK
  are deliberately absent (Holo's CJK_EXCLUDE_WIN).
- macOS: macOS Sonoma system fonts, including PingFang and Kefa, plus open
  developer/web fonts. A few Devanagari families are still absent.

Adobe's fonts (Minion Pro, Myriad Pro, the Adobe/Kozuka CJK and script
faces -- 185 files, 240 MB) were REMOVED on 2026-09-22: Adobe permits no
redistribution, and the fpgen corpus puts every one of those families on under
2% of real machines (Minion Pro 1.5%, the rest at 0.0%), so shipping them bought
no realism. The `adobe-cc` unit was dropped from scripts/data/font-manifests.json
in the same change, because reporting a family the bundle cannot render is a
reverse leak. Run scripts/font-metrics/licences.py for the current licence split.

The per-OS list of families the launcher may REPORT is generated from these
files, in this order, and checked by scripts/verify-fonts.py:

    scripts/gen-fonts-json.py     bundle + font-manifests.json -> fonts.json
                                  (--print-bases also prints the
                                  _ESSENTIAL_FONTS_* literals for
                                  pythonlib/camoufox/fingerprints.py, which are
                                  the INTERSECTION of that OS's version bases)
    scripts/gen-font-groups.py    fonts.json + font-manifests.json ->
                                  font-groups.json (the addition units, each
                                  with its own real-world probability) and
                                  font-bases.json (the OS-version bases with
                                  their weights)

Run both after the bundle or the manifest changes, in that order.


Disclaimer:

This project utilizes copyrighted fonts solely for academic and research purposes. The fonts used in this project are the intellectual property of their respective owners. No commercial use or distribution of these fonts is intended or permitted. All rights to the fonts are retained by their respective copyright holders. If you wish to use these fonts for any other purpose, please contact the copyright owners for appropriate permissions. If you wish a font to be removed from this repository, please open an issue.
