r"""
PublicKeyCredential.getClientCapabilities() answers for the claimed OS.

Stock Firefox fixes six capabilities at compile time by OS: largeBlob,
hybridTransport, passkeyPlatformAuthenticator and
userVerifyingPlatformAuthenticator are true on Windows and macOS and false on
Linux; credentialProtectionPolicy and enforceCredentialProtectionPolicy are
false on macOS only. A Linux build claiming Windows answered Linux's table.
Stock Firefox 158.0.1 on win-i9 (cfparity cross-os headed, 2026-10-30): the
four true; Camoufox Pro on Linux claiming that machine: false.

webauthn-capabilities-claimed-os.patch answers for the context's OS.

    python browser/tests/playwright/patches/webauthn-capabilities-claimed-os.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

PROBE = "() => PublicKeyCredential.getClientCapabilities()"
PLATFORM = ["extension:largeBlob", "hybridTransport", "passkeyPlatformAuthenticator",
            "userVerifyingPlatformAuthenticator"]
PROTECTION = ["extension:credentialProtectionPolicy", "extension:enforceCredentialProtectionPolicy"]
# os -> (the four platform keys, the two credential protection keys), as stock answers there.
EXPECTED = {"windows": (True, True), "macos": (True, False), "linux": (False, True)}


async def probe(binary, os_name):
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(headless=True, os=os_name, executable_path=str(binary),
                             i_know_what_im_doing=True) as browser:
        page = await browser.new_page()
        # A secure context: WebAuthn is not exposed on about:blank's opaque origin.
        await page.goto("https://example.com/")
        return await page.evaluate(PROBE)


def main():
    binary = resolve_binary()
    failures = []
    for os_name, (platform, protection) in EXPECTED.items():
        caps = asyncio.run(probe(binary, os_name))
        print(f"{os_name}: {caps}")
        for key in PLATFORM:
            if caps.get(key) is not platform:
                failures.append(f"{os_name}: {key} {caps.get(key)}, stock {platform}")
        for key in PROTECTION:
            if caps.get(key) is not protection:
                failures.append(f"{os_name}: {key} {caps.get(key)}, stock {protection}")
    for f in failures:
        print("FAIL", f)
    print("FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
