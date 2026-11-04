import pytest


@pytest.fixture(autouse=True)
def no_host_gpu_probe(monkeypatch):
    """launch_options launches the browser to read the host's GPU when an
    identity claims the host's OS. This suite has no browser, and its results
    must not depend on the machine running it, so the host reads as having no
    WebGL. Tests of the probe's consumers patch it themselves."""
    from camoufox import utils

    monkeypatch.setattr(utils, 'host_gpu', lambda *args: None)


@pytest.fixture(autouse=True)
def no_render_node(monkeypatch, tmp_path):
    """A Pro launch uses the render node this machine configured; the suite's
    must not, so no configuration is found. Render-node tests write their own."""
    from camoufox import render_node

    monkeypatch.delenv(render_node.CONFIG_ENV, raising=False)
    monkeypatch.setattr(render_node, 'DEFAULT_PATH', tmp_path / 'render-node.json')
