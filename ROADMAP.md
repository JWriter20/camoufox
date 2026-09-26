# Roadmap

What is planned, grouped by area. Items link to their issue. There are no
dates: an item ships when its tests prove it. To pick one up, comment on its
issue first.

## In progress

- **TypeScript/JavaScript package on npm**, at parity with `pythonlib`
  ([#784](https://github.com/daijro/camoufox/issues/784)).
- **Firefox 155** ([#764](https://github.com/daijro/camoufox/issues/764)).

## Identity

- **Draw more of the identity from fpgen.** Navigator, screen, window and
  headers come from fpgen today. Its fonts, voices, WebGL parameters,
  permissions, WebRTC capabilities and audio hashes are still drawn elsewhere.
- **Capture WebGL data for GPUs Camoufox cannot present yet**: Windows on ARM
  (Adreno), Direct3D 10-level hardware, and the Mesa/nouveau and older Intel
  Linux drivers. Presets naming them were dropped because nothing recorded
  their WebGL parameters. They come back as normal identities once real
  parameters for them exist.
- **Reproducible identities across relaunches**: the same seed gives the same
  device, including its canvas and audio output
  ([#442](https://github.com/daijro/camoufox/issues/442),
  [#765](https://github.com/daijro/camoufox/issues/765)).
- **Per-context fonts everywhere**: the default context
  ([#757](https://github.com/daijro/camoufox/issues/757)), and `measureText`
  agreeing with `@font-face local()`
  ([#759](https://github.com/daijro/camoufox/issues/759)).
- **Platform-consistent APIs**:
  - speech voices ([#717](https://github.com/daijro/camoufox/issues/717));
  - WebAuthn platform-authenticator availability
    ([#718](https://github.com/daijro/camoufox/issues/718));
  - audio output devices ([#768](https://github.com/daijro/camoufox/issues/768));
  - favicon caching ([#577](https://github.com/daijro/camoufox/issues/577)).

## Automation

- **Routing in the isolated world**: `route_web_socket`
  ([#775](https://github.com/daijro/camoufox/issues/775)) and server-sent events
  ([#786](https://github.com/daijro/camoufox/issues/786)).
- **Protocol resilience**: interrupting a runaway `evaluate`
  ([#720](https://github.com/daijro/camoufox/issues/720)), and recovering from a
  dead Juggler pipe ([#719](https://github.com/daijro/camoufox/issues/719)).

## Platforms

- Sandboxes without `ARCH_SET_GS`, such as gVisor
  ([#740](https://github.com/daijro/camoufox/issues/740)), and read-only
  filesystems ([#572](https://github.com/daijro/camoufox/issues/572)).
- **macOS**: headed windows must not take focus when shown
  ([#739](https://github.com/daijro/camoufox/issues/739)).
- **Windows**:
  - deterministic generic-font resolution at startup
    ([#783](https://github.com/daijro/camoufox/issues/783));
  - clean repaint while resizing
    ([#734](https://github.com/daijro/camoufox/issues/734)).

## Testing

- **Test the packaged browser in CI, langpacks included.** CI tests an
  unpackaged build that carries only `en-US`, so no suite can check another
  locale end to end.
- **Run the stealth grade on pull requests from forks.** Forks get no secrets
  today, so that check is skipped for them.
