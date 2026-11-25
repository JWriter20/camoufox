"""A name the Linux fontconfig rewrites to a font the identity has is a font the page sees.

browser/bundle/fontconfig/linux/fonts.conf carries the aliases a stock Ubuntu
ships (30-metric-aliases.conf and fonts-urw-base35's 61-urw-*.conf). Firefox
honours binding="same" aliases, so with C059 installed a page that probes
"Century" measures C059, on a real Ubuntu and on Camoufox alike. The identity
has to claim that name too: parity-checker's claim-fonts rule caught "Century"
rendering on every Linux identity without being in its font list.
"""

import json
import os
import xml.etree.ElementTree as ET

import pytest

HERE = os.path.dirname(__file__)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(HERE)))
CONF = os.path.join(REPO, 'browser', 'bundle', 'fontconfig', 'linux', 'fonts.conf')
FONTS = os.path.join(REPO, 'python', 'src', 'camoufox', 'fonts.json')

GENERICS = {'serif', 'sans-serif', 'sans serif', 'sans', 'monospace', 'mono', 'cursive', 'fantasy',
            'system-ui', 'emoji', 'math'}
# A name a real Ubuntu does not report (gen-fonts-json.py, verify-fonts.py warns).
NOT_REPORTED = {'Noto Sans Canadian Aboriginal Regular'}


def _same_binding_aliases():
    """(alias, first accepted/preferred family) for each binding="same" alias."""
    out = []
    for alias in ET.parse(CONF).getroot().iter('alias'):
        if alias.get('binding') != 'same':
            continue
        name = alias.findtext('family', '').strip()
        for tag in ('prefer', 'accept'):
            el = alias.find(tag)
            if el is not None and el.findtext('family'):
                out.append((name, el.findtext('family').strip()))
                break
    return out


@pytest.mark.skipif(not os.path.exists(CONF), reason='no fontconfig bundle in this checkout')
def test_every_rendering_alias_is_reportable():
    lin = set(json.load(open(FONTS, encoding='utf-8'))['lin'])
    missing = sorted(
        f'{name} -> {target}'
        for name, target in _same_binding_aliases()
        if target in lin and name not in lin and name.lower() not in GENERICS and name not in NOT_REPORTED
    )
    assert not missing, f'rendered through fonts.conf but never claimed: {missing}'
