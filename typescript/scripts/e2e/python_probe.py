#!/usr/bin/env python3
"""Launch the Python camoufox with a fixed identity and run the shared page probe.

Used by tests/e2e.test.ts to compare the TypeScript launcher against Python on
the same binary and the same identity:

    echo '{"mode": "headless", "url": "...", "kwargs": {...}}' | \
        .venv/bin/python typescript/scripts/e2e/python_probe.py

Prints one JSON object: {"probe": <probe result>, "config": <CAMOU_CONFIG>}.
`mode` is "config-only" (launch_options() alone), "headless" (Camoufox(...) -> new_page) or "persistent"
(Camoufox(persistent_context=True, user_data_dir=...)).
"""

import json
import sys
import tempfile
import warnings
from pathlib import Path

from camoufox.sync_api import Camoufox
from camoufox.utils import launch_options

PROBE = (Path(__file__).resolve().parent.parent.parent / 'tests' / 'fixtures' / 'e2e' / 'probe.js').read_text()


def config_of(options):
    env = options['env']
    chunks = sorted((int(k.rsplit('_', 1)[1]), v) for k, v in env.items() if k.startswith('CAMOU_CONFIG_'))
    return json.loads(''.join(v for _, v in chunks))


def main():
    req = json.loads(sys.stdin.read())
    kwargs = req['kwargs']
    warnings.simplefilter('ignore')
    config = config_of(launch_options(**kwargs))
    if req['mode'] == 'config-only':
        json.dump({'config': config}, sys.stdout)
        return
    with tempfile.TemporaryDirectory() as profile:
        extra = {'persistent_context': True, 'user_data_dir': profile} if req['mode'] == 'persistent' else {}
        with Camoufox(**kwargs, **extra) as browser:
            page = browser.new_page()
            page.goto(req['url'])
            probe = page.evaluate(PROBE)
    json.dump({'probe': probe, 'config': config}, sys.stdout)


if __name__ == '__main__':
    main()
