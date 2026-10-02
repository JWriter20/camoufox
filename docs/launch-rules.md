# What a build tells the launcher

Both launchers read two files from beside the browser binary: the managed
install's, or the caller's own when they pass `executable_path` (or set
`CAMOUFOX_EXECUTABLE_PATH`). On macOS they are read from
`Camoufox.app/Contents/Resources/`.

A build that compiles in extra features declares them in these files. The
launchers then configure it without knowing anything about that build, and
every other build is unaffected.

A Camoufox Pro build also ships `pro-build.json` there, which makes the
launchers start it with a lease: see [pro.md](pro.md).

## `properties.json`: the config keys the build reads

Every `CAMOU_CONFIG` key the build reads, with its type. A key that is not
listed is still sent, with a `Skipping unknown patch` line. A value of the
wrong type raises `InvalidPropertyType`.

A numeric property can declare a `min`. A configured value below it is raised
to it before launch:

```json
{ "property": "window.history.length", "type": "uint", "min": 2 }
```

## `launch.json`: prefs and environment the build needs at launch

Optional, and absent from the stock build. It is for a feature that is compiled
in but stays off until a pref or an environment variable turns it on:

```json
{
  "rules": [
    { "prefs": { "browser.sessionhistory.max_entries": 10 } },
    {
      "target": ["win"],
      "host": ["lin", "mac"],
      "prefs": { "example.feature": true },
      "env": { "EXAMPLE_FEATURE": "1" },
      "envFromConfig": { "EXAMPLE_ARCH": "example:arch" }
    },
    {
      "target": ["win"],
      "host": ["lin"],
      "envPaths": { "EXAMPLE_LIB": "lib/example.so" }
    }
  ]
}
```

- `prefs` are Firefox prefs.
- `env` sets environment variables verbatim.
- `envPaths` sets each variable to a file relative to the directory holding
  `launch.json`. The launch fails (`FileNotFoundError` in Python) if the file is
  missing: a feature the build needs and cannot find must not degrade quietly.
- `envFromConfig` sets each variable to the value of a config key, only when
  the identity has that key.

- `warn` is a message emitted as a `RuntimeWarning` when the rule applies. Use
  it for a pairing the build cannot support, so the caller hears about it
  instead of getting a quietly degraded browser.
- `exclusive: true` makes the rule own the variables it names in `env`,
  `envPaths` and `envFromConfig`. Where the rule does not apply, those
  variables are removed from the launch environment, the caller's included,
  unless another rule that does apply sets them.

A rule with `target` applies only when the identity's OS is one of those
listed, and a rule with `host` only when the machine running the browser is.
Both use `win`, `mac` and `lin`. A rule with neither always applies.

```json
{
  "rules": [
    {
      "target": ["win"],
      "host": ["lin"],
      "exclusive": true,
      "envPaths": { "EXAMPLE_LIB": "lib/example.so" }
    },
    {
      "target": ["win"],
      "host": ["mac"],
      "warn": "lib/example.so does not load on macOS; this identity renders without it."
    }
  ]
}
```

Where a rule applies, the caller wins. A pref passed in `firefox_user_prefs` is
never replaced, and neither is a variable that is already in the launch
environment: the process environment, or `env` when the caller passes one.
