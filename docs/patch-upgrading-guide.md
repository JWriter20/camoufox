# Firefox Patch Upgrading Guide

How to update Camoufox's patches when `upstream.sh` moves to a new Firefox
version. Patches break because Firefox renames APIs, moves code and shifts line
numbers; this guide covers finding and fixing those rejects.

## Table of Contents

1. [Understanding the Patch System](#understanding-the-patch-system)
2. [The Source Tree and Its Make Targets](#the-source-tree-and-its-make-targets)
3. [General Workflow](#general-workflow)
4. [Fixing Common Reject Types](#fixing-common-reject-types)
5. [Per-Context Machinery](#per-context-machinery)
6. [Testing and Validation](#testing-and-validation)
7. [Best Practices](#best-practices)

---

## Understanding the Patch System

### Patch Categories

All patches live under `patches/`, and `scripts/patch.py` applies every
`*.patch` in it (subdirectories included), sorted by file name:

- **Playwright**: `playwright/0-playwright.patch` (Juggler integration) and
  `playwright/1-leak-fixes.patch`. Their names sort first, and every other
  patch is written against a tree that already has them.
- **Feature patches**: `webrtc-ip-spoofing.patch`,
  `anti-font-fingerprinting.patch`, etc. Per-user-context (per-Playwright-context)
  support is built into each one.
- **`librewolf/`, `ghostery/`**: patches taken from those projects.

Compile-time dependencies between patches (MaskConfig, RoverfoxStorageManager)
are listed in [`patches/patch-dependencies.md`](../patches/patch-dependencies.md).

### Key Infrastructure Files

- **RoverfoxStorageManager.cpp/h**: Thread-safe key-value storage for per-context data
- **Manager Classes**: AudioFingerprintManager, WebRTCIPManager, etc.
- **Window.webidl**: Exposes the per-context setters to Playwright

---

## The Source Tree and Its Make Targets

The Firefox tree is `camoufox-<version>-<release>/` (from `upstream.sh`). It is
a git repository whose `unpatched` tag is plain Firefox plus `additions/` and
`settings/`. Run every target from the repository root:

| Target | What it does |
|---|---|
| `make dir` | Fetches and extracts Firefox if the tree is missing. Otherwise resets it to `unpatched`, runs `mach clobber` and `git clean -fdx` (the object directory goes too), re-copies additions, then applies every patch and lists the ones that left rejects. |
| `make revert` | `git reset --hard unpatched`. Untracked files stay, including new files that patches created. |
| `make clean` | `mach clobber`, `git clean -fdx`, then `make revert`: unpatched Firefox with nothing left over, without re-fetching. |
| `make patch ./patches/x.patch` | Applies one patch (`patch -p1`). |
| `make unpatch ./patches/x.patch` | Reverses one patch. |
| `make first-checkpoint` | Commits the current tree and tags it `first-checkpoint`. |
| `make workspace ./patches/x.patch` | Unapplies `x` if it is applied, runs `first-checkpoint`, then applies `x` again, so the working tree differs from the checkpoint by exactly that patch. |
| `make diff` | `git diff first-checkpoint`. Redirect it into the patch file. |

`git diff` does not show untracked files. Before `make diff`, mark new files
with `git add -N <file>` inside the source tree, or they will be missing from
the patch.

---

## General Workflow

### Step 1: Bump the Version and Find the Broken Patches

Update `version` and `release` in `upstream.sh`, then:

```bash
make dir
```

`patch.py` applies every patch and ends with a list of the ones that failed and
their reject files. It deletes the `.rej` files after listing them, so reproduce
each failure one patch at a time (Step 2).

### Step 2: Set Up One Patch

Start from a clean unpatched tree, apply what the patch builds on (at least the
Playwright patches, plus anything from `patches/patch-dependencies.md`),
checkpoint, then apply the broken patch. Use `make clean` rather than
`make revert` here: files that other patches created survive a revert and make
`patch` stop on "previously applied" prompts.

```bash
make clean
make patch ./patches/playwright/0-playwright.patch
make patch ./patches/playwright/1-leak-fixes.patch
make first-checkpoint
make patch ./patches/patch-name.patch     # fails, leaving .rej files
```

Find the reject files:

```bash
cd camoufox-<version>-<release>
find . -name '*.rej' -type f
```

### Step 3: Analyze Each Reject File

Read the reject file to understand what failed:

```bash
cat path/to/file.cpp.rej
```

Reject files show:
- `@@` lines: Line numbers where patch expected to apply
- `-` lines: What the patch expected to find (old code)
- `+` lines: What the patch wanted to add (new code)

### Step 4: Locate the Correct Position in Firefox Code

The line numbers in rejects are usually wrong for the new Firefox version. You need to:

1. **Search for unique context** around the reject
2. **Understand what the patch is doing**
3. **Find equivalent location** in new Firefox code

### Step 5: Apply Changes Manually

Edit the file to make the rejected change at the correct location.

### Step 6: Remove Reject Files

After fixing all rejects, delete the `.rej` files and any `.orig` backups
`patch` left, so they do not end up in the diff:

```bash
find . -name '*.rej' -o -name '*.orig' | xargs rm -f
```

### Step 7: Write the Updated Patch

From the repository root:

```bash
(cd camoufox-<version>-<release> && git add -N path/to/new/file.cpp)   # new files only
make diff > patches/patch-name.patch
```

### Step 8: Verify

Run `make dir` again. The patch should no longer be listed as failing.

---

## Fixing Common Reject Types

### Type 1: Include Directive Rejects

**Symptom**: Reject shows failed `#include` additions

**Example Reject**:
```
@@ -325,6 +325,7 @@
 #include "xpcpublic.h"

+#include "WebRTCIPManager.h"
 #include "nsDocShell.h"
```

**How to Fix**:

1. Read the actual file to find the includes section
2. Search for nearby includes (e.g., `xpcpublic.h`)
3. Add the new include in the appropriate location
4. Firefox include order: system headers, then Mozilla headers, alphabetically within groups

**Example**:

```cpp
// Find this in the actual file:
#include "xpcpublic.h"

// Add the missing includes after it:
#include "xpcpublic.h"

#include "WebRTCIPManager.h"
#include "nsDocShell.h"
#include "mozilla/OriginAttributes.h"
```

### Type 2: Function Signature Changes

**Symptom**: Reject shows function call with changed parameters

**Example Reject**:
```
-  mouseOrPointerEvent.mButton = aButton;
+  mouseOrPointerEvent.mJugglerEventId = aMouseEventData.mJugglerEventId;
```

**Common Causes**:
- Firefox refactored the API
- Parameters moved from individual args to struct/data object
- Parameter order changed

**How to Fix**:

1. Search for the function definition in Firefox source
2. Understand the new API structure
3. Port the patch logic to the new API

**Example - Firefox 146 Mouse Event Refactoring**:

Old Firefox 144 API (individual parameters):
```cpp
void SynthesizeMouseEvent(int x, int y, int button, ...)
```

New Firefox 146 API (structured data):
```cpp
void SynthesizeMouseEvent(SynthesizeMouseEventData& aData,
                         SynthesizeMouseEventOptions& aOptions)
```

Port the patch:
```cpp
// Old patch code:
mouseEvent.mButton = aButton;
mouseEvent.jugglerEventId = aJugglerEventId;

// New patch code for Firefox 146:
mouseOrPointerEvent.mButton = aMouseEventData.mButton;
mouseOrPointerEvent.mJugglerEventId = aMouseEventData.mJugglerEventId;
mouseOrPointerEvent.convertToPointer = aOptions.mConvertToPointer;
```

### Type 3: Missing Context - Code Moved

**Symptom**: Reject shows context that doesn't exist in the file

**How to Fix**:

1. Use grep to search for unique function names or variables in the reject
2. Find where Firefox moved the code
3. Apply the patch to the new location

```bash
# Search across the codebase
grep -r "FunctionName" camoufox-<version>/ --include="*.cpp"
```

### Type 4: New Parameter Added to Function Calls

**Symptom**: Reject shows function call, but Firefox added/removed parameters

**Example - MakeTextRun userContextId**:

Old call:
```cpp
MakeTextRun(text, len, drawTarget, appUnitsPerDevPixel, flags, recorder);
```

New Firefox expects:
```cpp
MakeTextRun(text, len, drawTarget, appUnitsPerDevPixel, flags, recorder, userContextId);
```

**How to Fix**:

1. Extract userContextId from available context (Document, PresContext, etc.)
2. Add proper extraction code before the call
3. Pass userContextId as the last parameter

**Standard userContextId Extraction Pattern**:

```cpp
uint32_t userContextId = 0;
if (mozilla::dom::Document* doc = presContext->Document()) {
  if (nsIPrincipal* principal = doc->NodePrincipal()) {
    auto* bp = mozilla::BasePrincipal::Cast(principal);
    if (bp) {
      userContextId = bp->OriginAttributesRef().mUserContextId;
    }
  }
}

// Now pass userContextId to the function
MakeTextRun(..., userContextId);
```

### Type 5: Line Number Shifts (No Code Changes)

**Symptom**: Reject shows patch tried to apply at wrong line number, but code is identical

**How to Fix**:

Simply apply the patch manually at the correct line number. The code hasn't changed, just the location.

---

## Per-Context Machinery

Most spoofing patches carry per-context support. When porting one, expect these
pieces:

1. **Manager classes** (e.g., AudioFingerprintManager, WebRTCIPManager):
   - Store per-context settings using RoverfoxStorageManager
   - Provide WebIDL-compatible enable/disable checks
   - Handle self-destructing functions

2. **Window.webidl functions**:
   - JavaScript APIs exposed to Playwright
   - Examples: `setAudioFingerprintSeed()`, `setWebRTCIPv4()`

3. **nsGlobalWindowInner.cpp implementations**:
   - Extract userContextId from window/document/docshell
   - Call manager classes
   - Self-destruct logic (remove function after first use)

4. **Core logic changes**:
   - Consult the per-context manager before the global config (MaskConfig)
   - Pass userContextId through call chains

See [`per-context-patches.md`](per-context-patches.md) for the full list.

---

## Testing and Validation

### Minimal Verification

After updating a patch, always verify:

1. **Every patch applies cleanly**: `make dir` lists no failures.

2. **Build compiles** (if feasible):
   ```bash
   make build
   ```

### Full Testing

Run the suites that cover the patch (see [`ci/README.md`](../ci/README.md)):
`python3 -m ci.run_patch_guards --binary <camoufox-bin>` is the most direct
evidence that a patch which still applies was not neutered by the upgrade.

---

## Best Practices

### DO:

1. ✅ **Start each patch from `make clean`** plus the patches it builds on
2. ✅ **Read and understand** what the patch is trying to do before fixing rejects
3. ✅ **Search for API changes** in Firefox release notes when functions have changed
4. ✅ **Use grep/search** extensively to find where code moved
5. ✅ **Extract userContextId properly** using the standard pattern
6. ✅ **Check with `make dir`** that the whole stack applies before considering a patch done
7. ✅ **Keep commits atomic** - one patch fix per session
8. ✅ **Document major API changes** you discover

### DON'T:

1. ❌ **Don't hand-edit `.patch` files** - edit the tree and regenerate with `make diff`
2. ❌ **Don't leave TODO comments** - fix things properly as you go
3. ❌ **Don't guess parameter values** - extract them properly or investigate
4. ❌ **Don't skip verification** - always test the patch applies cleanly
5. ❌ **Don't batch multiple patch updates** - do them one at a time
6. ❌ **Don't assume line numbers are correct** in reject files
7. ❌ **Don't ignore warnings** during patch application

### Common Pitfalls

1. **Assuming reject line numbers are accurate**: They're usually wrong in new Firefox versions
2. **Not understanding API changes**: Firefox refactors often - read the new code
3. **Forgetting new files**: `git diff` skips untracked files; `git add -N` them before `make diff`
4. **Diffing against the wrong base**: `make first-checkpoint` before applying the patch you are fixing, or `make diff` will include its dependencies
5. **Leaving reject files**: Remove all `.rej` and `.orig` files after fixing

---

## Appendix: Firefox Source Navigation

### Finding Files

```bash
# Find files by name
find . -name "Navigator.cpp" -type f

# Find files containing a symbol
grep -r "GetAcceptLanguages" . --include="*.cpp"

# Find class definitions
grep -r "class Navigator" . --include="*.h"
```

### Understanding Firefox Code Structure

- `dom/`: DOM implementation
  - `dom/base/`: Core DOM classes (Window, Document, Navigator, etc.)
  - `dom/webidl/`: WebIDL interface definitions
  - `dom/media/webrtc/`: WebRTC implementation
- `gfx/`: Graphics and font rendering
  - `gfx/thebes/`: Text rendering (fonts, glyphs, shaping)
- `layout/`: Layout engine
  - `layout/generic/`: Text frames
  - `layout/mathml/`: MathML rendering

### Common Firefox Patterns

**User Context ID Extraction**:
```cpp
uint32_t userContextId = 0;
if (Document* doc = GetDocument()) {
  if (nsIPrincipal* principal = doc->NodePrincipal()) {
    auto* bp = mozilla::BasePrincipal::Cast(principal);
    if (bp) {
      userContextId = bp->OriginAttributesRef().mUserContextId;
    }
  }
}
```

**Three-tier userContextId fallback** (for WebIDL functions):
```cpp
// 1) Document's principal (preferred)
if (Document* doc = win->GetDoc()) {
  if (nsIPrincipal* p = doc->NodePrincipal()) {
    userContextId = p->OriginAttributesRef().mUserContextId;
  }
}

// 2) DocShell origin attributes
if (userContextId == 0) {
  if (nsIDocShell* ds = win->GetDocShell()) {
    auto* concrete = static_cast<nsDocShell*>(ds);
    userContextId = concrete->GetOriginAttributes().mUserContextId;
  }
}

// 3) Top browsing context
if (userContextId == 0) {
  if (BrowsingContext* bc = win->GetBrowsingContext()) {
    RefPtr<BrowsingContext> top = bc->Top();
    // ... extract from top window
  }
}
```

---

## Summary Checklist

When updating patches for a new Firefox version:

- [ ] Bump `upstream.sh` and run `make dir` to list the failing patches
- [ ] For each: `make clean`, apply its dependencies, `make first-checkpoint`, `make patch` it
- [ ] Analyze each reject to understand what changed
- [ ] Search Firefox source for moved/refactored code
- [ ] Fix rejects by porting logic to new Firefox APIs
- [ ] Extract userContextId properly using standard patterns
- [ ] Don't leave TODO comments - fix everything immediately
- [ ] Remove all `.rej` and `.orig` files after fixing
- [ ] `git add -N` any new files
- [ ] `make diff > patches/<name>.patch`
- [ ] `make dir` applies the whole stack cleanly
- [ ] Document any major API changes discovered

---

## Additional Resources

- Firefox source: https://searchfox.org/
- Firefox API documentation: https://firefox-source-docs.mozilla.org/
- Mercurial repository: https://hg.mozilla.org/mozilla-central/
