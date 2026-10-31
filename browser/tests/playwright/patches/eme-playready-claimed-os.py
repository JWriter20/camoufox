r"""
A Windows identity's EME answers as Windows Firefox's.

Stock Firefox 158.0.1 on win-i9, cfparity media.eme request (cenc, avc1.42E01E,
mp4a.40.2, robustness ''), 6/6 launches:

    com.widevine.alpha                       access (not-allowed, not-allowed, temporary)
    com.microsoft.playready.recommendation   NotSupportedError "Key system configuration is not supported"

A Linux build said "EME has been preffed off" for Widevine (Linux builds default
media.eme.enabled off; the launcher now turns it on for a Windows identity) and
"Key system is unsupported" for PlayReady, which Windows has through Media
Foundation (eme-playready-claimed-os.patch). A Linux identity keeps Linux's
answers. Widevine downloads its CDM on first use, so it needs the network.

    python browser/tests/playwright/patches/eme-playready-claimed-os.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

PROBE = """async (ks) => {
  const cfg = [{ initDataTypes: ['cenc'],
    videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"', robustness: '' }],
    audioCapabilities: [{ contentType: 'audio/mp4; codecs="mp4a.40.2"', robustness: '' }] }];
  return Promise.race([
    navigator.requestMediaKeySystemAccess(ks, cfg).then((a) => {
      const c = a.getConfiguration();
      return ['access', c.distinctiveIdentifier, c.persistentState, c.sessionTypes.join(',')].join(' ');
    }, (e) => `${e.name} ${e.message}`),
    new Promise((r) => setTimeout(() => r('timeout'), 30000))]);
}"""
WIDEVINE, PLAYREADY = "com.widevine.alpha", "com.microsoft.playready.recommendation"
EXPECTED = {
    "windows": {WIDEVINE: "access not-allowed not-allowed temporary",
                PLAYREADY: "NotSupportedError Key system configuration is not supported"},
    "linux": {WIDEVINE: "NotSupportedError EME has been preffed off",
              PLAYREADY: "NotSupportedError Key system is unsupported"},
}


async def probe(binary, os_name):
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(headless=True, os=os_name, executable_path=str(binary),
                             i_know_what_im_doing=True) as browser:
        page = await browser.new_page()
        # EME needs a secure context; the page itself never reaches the network.
        await page.route("https://eme.test/", lambda r: r.fulfill(status=200, content_type="text/html",
                                                                  body="<body></body>"))
        await page.goto("https://eme.test/")
        return {ks: await page.evaluate(PROBE, ks) for ks in (WIDEVINE, PLAYREADY)}


def main():
    binary = resolve_binary()
    failures = []
    for os_name, want in EXPECTED.items():
        got = asyncio.run(probe(binary, os_name))
        print(f"{os_name}: {got}")
        for ks, answer in want.items():
            if got[ks] != answer:
                failures.append(f"{os_name} {ks}: {got[ks]!r}, stock {answer!r}")
    for f in failures:
        print("FAIL", f)
    print("FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
