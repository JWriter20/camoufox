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

## What a lease grants

Besides the lease itself, the API can grant a browser four things. Each is a
section of the lease, and the launcher applies each one it gets. Four launch
options shape what is asked for; each is left out of the request unless you
set it:

| Option | Values | Asks for |
|---|---|---|
| `profile` | a name, up to 64 characters | the [profile](#profiles) of that name, created on first use |
| `warm_plan` | `"none"`, `"standard"`, `"continuous"` | the plan a new profile is created with (with `profile` only) |
| `egress` | `false`, or `{class, country, sticky}` | no [managed egress](#managed-egress), or the egress wanted |
| `gpu` | `false` | no [remote GPU](#remote-rendering): render on this machine |

They need a Pro build; on a stock build, or with a `CAMOU_LEASE_FILE` of your
own, they raise `ValueError`.

### Managed egress

Unless you pass your own `proxy` or `egress: false`, the lease carries a proxy
for the browser, and the launcher routes the browser through it:

```ts
const browser = await Camoufox({
	executable_path: "/path/to/camoufox-pro/camoufox-bin",
	egress: { class: "residential", country: "US" },
});
```

`class` is one of `residential`, `isp`, `datacenter` and `mobile`, and
`country` an ISO 3166 code such as `US`. Left out, the plan's default is used.
The launcher sets `geoip` to the proxy's exit address when the API knows it,
and otherwise looks the exit up through the proxy, so timezone, locale and
WebRTC match the exit. A `geoip` you pass yourself wins. `localhost`,
`127.0.0.1`, `::1` and `*.local` bypass the proxy, which refuses local
destinations.

Your own `proxy` is never replaced: with one, the launcher asks for no managed
egress.

### Remote rendering

A Windows identity renders WebGL, WebGPU and canvas on a remote Windows GPU
unless you pass `gpu: false`. The launcher writes the lease's render document
to a file only your user can read, beside the lease file, points the browser
at it in `RENDERFARM_FIREFOX_CONFIG`, and sets the prefs the lease names. It
rewrites the file at each renewal and deletes it when the lease ends. When the
API cannot serve a remote GPU the launch fails with `GpuUnavailable`; retry
after its `retry_after`, or pass `gpu: false`.

### Profiles

A profile is an identity that stays the same for its whole life, together with
its browser state: cookies, local storage, IndexedDB, logins and history.

```ts
const context = await Camoufox({
	executable_path: "/path/to/camoufox-pro/camoufox-bin",
	os: "windows",
	profile: "linkedin-01",
	warm_plan: "none",
	proxy: { server: "http://my.proxy:8080" },
});
const page = await context.newPage();
await page.goto("https://example.com");
await context.close(); // resolves once the state is synced
```

```python
from camoufox.sync_api import Camoufox

with Camoufox(
    executable_path="/path/to/camoufox-pro/camoufox-bin",
    os="windows",
    profile="linkedin-01",
    warm_plan="none",
    proxy={"server": "http://my.proxy:8080"},
) as context:
    context.new_page().goto("https://example.com")
```

A profile launch is a persistent context, so `Camoufox()` returns a
`BrowserContext`. It needs `os`, one OS, which must be the profile's; the
profile's identity comes from the API and is applied whole, so pass no
`config`, `fingerprint`, `fingerprint_preset` or `user_data_dir` with it.
`launch_server()` does not take a profile.

The browser state of a `warm_plan: "none"` profile syncs through this machine:

1. before the launch, the launcher downloads the profile's last state, checks
   and decrypts it, and writes it to a fresh user-data directory under the
   camoufox cache (`camoufox/pro/profiles/<profile id>/sessions/`);
2. when the context closes, it collects what is worth keeping (not caches,
   telemetry or lock files), encrypts it, uploads what changed, and commits it
   as the next version, which also releases the lease; `close()` returns after
   that, and the directory is deleted.

`"none"` is the default for a new profile launched with your own `proxy`. A
`"standard"` or `"continuous"` profile is kept warm in the cloud: it launches
with its identity, on an empty directory, and its state does not sync to this
machine (a warning says so). A profile asked for with another `os` or
`warm_plan` than it has fails with `ProfileMismatch`.

**The content key.** State is encrypted on this machine with your account's
content key, which never leaves your machines: the API stores only ciphertext
it cannot read. The launcher creates the key the first time, at
`pro-content-keys/<account id>.key` beside the key file (mode `0600`), and
says so. Copy that file to every machine that launches the account's profiles,
or set `CAMOUFOX_PRO_CONTENT_KEY` to its contents. There is no recovery: state
synced with a lost key cannot be read again.

**When a sync cannot commit**, the state of that session is moved aside and
kept, and a warning names the place:

| Kept under | When |
|---|---|
| `conflicts/` | Another machine committed the profile since this session started, or the lease no longer holds the profile. A conflict is never merged |
| `pending/` | Another lease holds the profile now, the state is over 1 GiB, or the sync failed |

Both are under `camoufox/pro/profiles/<profile id>/` in the camoufox cache. The
launcher does not retry them later. `close()` raises when the sync failed for
any other reason, and `StatePoolSealed` when the API holds the profile's state
for the warm pool. State written by a newer Firefox than the build's is never
opened: the launch fails with `StateNewerThanBrowser`.

In Python, profile sync needs the `pro` extra:

```bash
pip install "camoufox[pro]"
```

### The captcha solver

When the lease includes captcha solving, the browser (or persistent context)
carries it as `.pro.captcha`: `endpoint`, `remaining` and `expires_at`.
`endpoint` is an OpenAI-compatible API base URL. Call it with your own captcha
key; the lease carries none:

```ts
const browser = await Camoufox({ executable_path: "/path/to/camoufox-pro/camoufox-bin" });
const captcha = browser.pro?.captcha;
if (captcha) {
	const models = await fetch(`${captcha.endpoint}/models`, {
		headers: { Authorization: `Bearer ${process.env.CAPTCHA_KEY}` },
	});
	console.log(await models.json());
}
await browser.close();
```

`.pro.leaseId` (`.pro.lease_id` in Python) is the lease's id. `remaining` is
the solves left when the lease was minted.

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
| `ProfileMismatch` | The profile exists with another `os` or `warm_plan`, or another egress regime, than the launch asked for | Launch it as it was created, or use another profile name |
| `GpuUnavailable` | No remote GPU can serve this Windows identity now | Retry after `retry_after`, or pass `gpu: false` |
| `StatePoolSealed` | The profile's state is held for the warm pool, so it cannot sync through this machine | Launch it without expecting its state, or use a `warm_plan: "none"` profile |
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

## Decisions

Choices made while building the lease sections into the launchers, with the
reason for each.

- **Option names** are the API's own field names: `profile`, `warm_plan`,
  `egress`, `gpu`. `pro_key` keeps its prefix because `key` alone would be
  ambiguous.
- **Nothing is sent that you did not set**, except `egress: false` with your
  own `proxy`: the API applies the plan's defaults otherwise.
- **A profile launch needs one explicit `os`.** The API refuses a profile
  asked for with another OS, and drawing one at random would fail half the
  launches of an existing profile.
- **The profile's identity is applied whole**, with `i_know_what_im_doing`, its
  Firefox major substituted for `{FF}`, and no default addons, so nothing is
  drawn per launch. Timezone and locale come from `geoip` through the exit the
  browser runs behind.
- **A profile runs in a directory the launcher owns** and deletes after a
  successful sync, so a stale directory is never launched by mistake.
- **The content key is its own file**, one per account, not a field of
  `pro-credentials.json`: `camoufox logout` deletes the credentials, and must
  not destroy the only copy of the key. It is created with a link that fails
  if the file exists, so two launches starting together never make two keys.
- **SQLite is checkpointed only where a write-ahead log is pending.** A browser
  that shut down cleanly leaves none; a database whose log cannot be folded
  travels with it and is marked suspect. Restore does not re-check databases:
  every chunk is authenticated and its id recomputed before it is written.
  In Node this uses `node:sqlite`, which prints an ExperimentalWarning the
  first time it loads.
- **A capture that cannot commit is kept, never merged and never retried
  automatically.** Merging two browser profiles is not safe; a person decides.
- **`crashed` is always reported `false`:** the launcher cannot tell a crash
  from a window closed by hand.
- **The bundle is checked against the sha256 the lease names**, not against
  its signature: the launcher holds no lease-signing key, and the lease itself
  came from the API over TLS.
- **`GpuUnavailable` is raised at once**, with the API's `retry_after`, rather
  than retried: the launch would otherwise block for an unknown time.
- **Prefs the lease sets are treated as the caller's**, so a rule in the
  build's `launch.json` never replaces them.
- **A lease minted again after the API lost it carries a new egress
  credential** the running browser cannot pick up; the launcher warns instead
  of restarting the browser.
- **Dependencies.** Node: XChaCha20-Poly1305 is not in `node:crypto`, so
  `@noble/ciphers` (pinned to 2.4.0) seals; HKDF, HMAC, SHA-256 and zstd come
  from Node itself, and FastCDC is implemented in the package, checked against
  the shared vectors. Python: the optional `pro` extra (`pynacl`, `zstandard`,
  `pyfastcdc`), imported only for a profile launch.
