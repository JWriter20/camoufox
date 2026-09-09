You are rebasing Camoufox's patch stack from Firefox {{OLD_VERSION}} onto Firefox
{{NEW_VERSION}}. This is turn {{TURN}}.

{{FAILED_COUNT}} patch(es) do not apply: {{FAILED_LIST}}

## What this repository is

Camoufox is not a Firefox fork you edit directly. It is a build system that
fetches upstream Firefox, applies ~49 patches from `patches/`, and produces an
anti-detect browser. Fingerprint spoofing happens in C++ and in Juggler, not in
injected JavaScript, which is the whole reason it is not visible to a page.

The `camoufox-*/` directory is **generated**. Editing it is how you test a fix;
it is not how you keep one. A fix only counts once it is written back into a
file under `patches/`.

## Read these first

- `harness/TRIBAL-KNOWLEDGE.md` — what to do and what not to do here. Read it
  before you touch anything. It is short and it is the accumulated result of
  previous upgrades going wrong.
- `docs/patch-upgrading-guide.md` — reject types you will hit and how each is
  fixed, with the `userContextId` extraction patterns spelled out.
- `docs/playwright-maintenance.md` — only if a patch under `patches/playwright/`
  or a file under `additions/juggler/` is involved.

## Your task

For each rejecting patch:

1. Read the reject hunks below. The line numbers in them are wrong — they are
   from the old Firefox. Ignore them and find the code by searching.
2. Work out what the patch is trying to achieve. Do not pattern-match the diff;
   understand the behaviour, because Firefox has usually moved or restructured
   the code rather than deleted it.
3. Apply the change by hand at the correct place in `camoufox-{{NEW_VERSION}}-*/`.
4. Regenerate the patch file from the tree, including any new files:

   ```bash
   cd camoufox-{{NEW_VERSION}}-*/
   git add path/to/any/new/file.cpp
   git diff --cached --binary > /tmp/name.patch
   git diff --binary >> /tmp/name.patch
   cp /tmp/name.patch ../patches/name.patch
   ```

Work one patch at a time. The harness resets the tree and re-applies the whole
stack after your turn, so a half-finished patch is worse than an untouched one.

## Rules

- **You may edit**: `patches/`, `additions/`, `settings/`, `upstream.sh`, `docs/`.
- **You may not edit**: `harness/`, `tests/`, `build-tester/`, `service-tester/`,
  `.github/workflows/`. These hold the gates that judge your work and the
  baseline they judge against. Writes there are detected after every turn and
  void the entire run — not just your change. If a test genuinely looks wrong,
  say so in your output and leave it alone.
- **Never** use `git reset` or `git clean` inside `camoufox-*/`. It deletes
  untracked files the build needs. Use `make clean` or the harness's own reset.
- **Never** leave a `TODO`, a stub, or a commented-out hunk. A patch that
  applies but does nothing is worse than one that fails loudly: it passes the
  patch gate and then silently removes a spoofing behaviour, which the stealth
  gate may not catch until someone's scraper is banned.
- **Never** guess a parameter value to make something compile. If a call now
  needs a `userContextId`, extract it properly — the guide gives the three-tier
  fallback pattern.
- Do not delete or disable a patch to make the stack apply. If a patch is
  genuinely obsolete because Firefox now does the thing natively, say that
  explicitly in your output and leave the patch in place for a human to remove.

## Rejects

{{REJECTS}}
