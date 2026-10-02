# Camoufox Pro in the launchers

A Camoufox Pro build is a Camoufox browser with extra features that only run
for a paying account. It verifies a signed **lease** when it starts, and it
refuses to start without a valid one. Both launchers, Python and TypeScript,
get that lease for you. A stock build is unaffected: no request is made and
nothing below applies.

## Which builds are Pro builds

A Pro build ships `pro-build.json` beside its executable (in
`Camoufox.app/Contents/Resources/` on macOS), next to the files described in
[launch-rules.md](launch-rules.md):

```json
{ "build_hash": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "version": "156.0.1-pro.1", "target": "linux-x86_64" }
```

`build_hash` identifies the release to the Camoufox Pro API. A hash the API
does not list is refused with `BuildNotAllowlisted`.

## Signing in

```bash
camoufox login
```

prints a link and a code. Open the link, sign in, and confirm the code. The
launcher then creates a Camoufox Pro key (`cfp_live_...`) for this machine and
stores it where only your user can read it:

| OS | Key file |
|---|---|
| Linux | `~/.config/camoufox/pro-credentials.json` (or under `$XDG_CONFIG_HOME`) |
| macOS | `~/Library/Application Support/camoufox/pro-credentials.json` |
| Windows | `%LOCALAPPDATA%\camoufox\camoufox\pro-credentials.json` |

The file is mode `0600` in a `0700` directory; on Windows its ACL grants only
your account. A key file other users can read is not used: the launch fails with
the `chmod 600` command that fixes it.

`camoufox logout` deletes the stored key. The key stays valid until you revoke
it in the Camoufox Pro dashboard, so revoke it there too if the machine is being
handed on.

Both launchers read the same file, so signing in with either CLI signs in both.

### Without the CLI

On a CI runner or in a container, create a key in the dashboard and pass it in
the environment instead:

```bash
export CAMOUFOX_PRO_KEY=cfp_live_...
```

or to one launch, as `pro_key`. The key is taken from the first of these that
is set: the `pro_key` argument, `CAMOUFOX_PRO_KEY`, the key file.

`CAMOUFOX_PRO_API` points both launchers at a different Camoufox Pro API. It
defaults to `https://api.camoufox.com`.

## Checking a machine

```bash
camoufox pro --activate
```

mints a lease for the local Pro build, prints what it grants, and releases it
straight away, without starting the browser. Use it after `camoufox login` on a
new machine, or in a CI job before the real work. It exits 0 only when the API
granted the lease:

```
[ ok ] lease verified: lse_0b7c... for 156.0.1-pro.1, windows identity, layout fidelity
[ -- ] profile: not granted
[ ok ] egress: granted (residential, US)
[ -- ] gpu: not granted
[ ok ] captcha: granted (250)
[ ok ] lease released
```

Each line after the first is one section of the lease. `[ ok ]` means the lease
carries it, and `[ -- ] ... not granted` means the API left it out. A granted
section's credentials are never printed. A refused lease prints one
`[FAIL] lease:` line with the API's reason, the same as the matching
[launch error](#when-a-launch-fails), and a missing key or a build without
`pro-build.json` prints `[FAIL]` too; each exits 1.

The build is the one beside `--executable-path`, else beside
`CAMOUFOX_EXECUTABLE_PATH`, else the active install. `--os windows|macos|linux`
picks the identity OS to lease for, and defaults to this machine's. The key and
API come from the same places as for a launch (below).

## Launching

Nothing changes in your code. Point the launcher at the Pro build:

```python
from camoufox.sync_api import Camoufox

with Camoufox(executable_path="/path/to/camoufox-pro/camoufox-bin") as browser:
    page = browser.new_page()
    page.goto("https://example.com")
```

```ts
import { Camoufox } from "@camoufox/camoufox";

const browser = await Camoufox({
	executable_path: "/path/to/camoufox-pro/camoufox-bin",
});
const page = await browser.newPage();
await page.goto("https://example.com");
await browser.close();
```

For every Pro browser it launches, the launcher:

1. mints a lease from the API for the identity's OS;
2. writes it to a file only your user can read and passes its path to the
   browser in `CAMOU_LEASE_FILE`;
3. renews it about once a minute while the browser runs, rewriting the file;
4. releases it and deletes the file when the browser closes.

Each browser holds its own lease, and each lease is one of the concurrent
browsers your plan includes. Leases are renewed from a daemon thread in Python
and an unreferenced timer in Node, so they work with the sync and async APIs
and never keep a process alive.

A lease is released when its browser or persistent context closes, when
`launch_server()` returns, and when the process exits normally (including on
Ctrl-C in Python). A process that is killed cannot release: its lease stops
being renewed and the API frees the slot about half an hour later. Its lease
file is deleted by the next launch after 24 hours.

`launch_options()` / `launchOptions()` mints the lease itself, so options built
by hand work too. Such a lease is released when the options are launched through
`NewBrowser`, `Camoufox` or `launch_server` and that browser closes, or at
process exit otherwise. Launch each set of options once: a second browser needs
its own lease, so call `launch_options()` again.

If the launch environment already has `CAMOU_LEASE_FILE`, the launcher takes it
as a lease you manage yourself and mints nothing.

## When a launch fails

Every error below is a `ProError`, importable from `camoufox.exceptions` in
Python and from `@camoufox/camoufox` in TypeScript. Each carries the API's
`code`, its `resolution_url` when there is one, and `details`. In Python,
`message` is the API's explanation alone; in TypeScript it is `detail`.

| Exception | Meaning | What to do |
|---|---|---|
| `NotSignedIn` | No key was found, or the API does not accept it | Run `camoufox login`, or set `CAMOUFOX_PRO_KEY` |
| `SubscriptionRequired` | The account has no active Camoufox Pro plan | Subscribe at the `resolution_url` |
| `AllowanceExhausted` | A metered allowance is used up and usage-based billing is off | Top up, or turn usage-based billing on |
| `AccountSuspended` | The account is suspended | See the `resolution_url` |
| `BuildNotAllowlisted` | The browser is not a published Pro release, or was revoked | Install a current Pro release |
| `LeaseLimitReached` | Every concurrent browser in the plan is running. The message names the machines holding them | Close one, or add browsers to the plan. The launcher never waits for a slot |
| `CapabilityMismatch` | This machine cannot present the requested identity | Launch it on a machine that can |
| `InvalidRequest` | The API rejected the request | Report it: the launcher sent something the API does not accept |
| `RateLimited` | The API asked the launcher to slow down, and kept asking through three retries | Launch fewer browsers at once |
| `ProUnavailable` | The API could not be reached, or failed, through three retries (1, 2 and 4 seconds apart) | Check the network; see the API status |
| `ProClockSkew` | This machine's clock is more than 3 minutes off the API's, so a fresh lease would look expired | Fix the clock (NTP). Over one minute off logs a warning |
| `LeaseRefused` | The browser refused the lease it was started with, and exited with status 78. `reason` is the browser's own | Report it with the reason |

The launch fails instead of starting the browser without a lease, because a Pro
build does not run without one.

### While the browser runs

A renewal that cannot reach the API is retried after 5, 10, 20, 40 and then
every 60 seconds; the lease stays valid meanwhile. After three minutes of
failures the launcher logs a warning (the `camoufox.pro` logger in Python,
`console.warn` in Node). If the API answers that the lease is gone, the launcher
mints one new lease into the same file. If it refuses the key or the account,
renewal stops and logs an error, and the browser exits when its lease runs out,
with `camoufox-pro: lease expired (drain elapsed)` on standard error.
