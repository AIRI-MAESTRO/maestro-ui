"""Catch omitted SEA client metadata before any platform archive can pass."""

import json
from pathlib import Path

import pytest

from care.runtime.desktop_smoke import verify_client_bootstrap

CORE = "@deepseek-ai/dsh-client-modules"
SETTINGS = "@maestro-care/dsh-client-ui-settings"
BRANDING = "@maestro-care/dsh-client-ui-branding"


def graph():
    return {
        "entries": [{"id": name} for name in (CORE, SETTINGS, BRANDING)],
        "batches": [
            {"phase": "bootstrap", "url": "/plugins/??core/client.js&rev=1", "entries": [CORE]},
            {"phase": "application", "url": "/plugins/??app/client.js&rev=2", "entries": [SETTINGS, BRANDING]},
        ],
    }


def html(manifest, *, script=True):
    preload = "" if not script else "".join(
        '<script src="' + batch["url"].replace("&", "&amp;") + '"></script>'
        for batch in manifest["batches"] if batch["phase"] == "bootstrap"
    )
    return '<html><head>' + preload + '<script>globalThis["__DSH_BOOT__"] = ' + json.dumps(manifest) + '</script></head></html>'


def fetcher(manifest, *, status=200, content_type="text/javascript", omit=None):
    requested = []

    def fetch(path):
        requested.append(path)
        batch = next(batch for batch in manifest["batches"] if batch["url"] == path)
        body = "\n".join(
            'window.__ModuleLoader__.load({ id: ' + json.dumps(name) + ', factory: () => ({}) })'
            for name in batch["entries"] if name != omit
        ).encode()
        return status, content_type, body

    return fetch, requested


def test_complete_client_graph_fetches_actual_registration_batches():
    manifest = graph()
    fetch, requested = fetcher(manifest)
    verify_client_bootstrap(html(manifest), fetch)
    assert requested == [batch["url"] for batch in manifest["batches"]]


def test_actual_sealed_windows_graph_is_rejected_before_ui_launch():
    # Actual provider-free Windows run38755247364, graph only; no auth data.
    manifest = json.loads((Path(__file__).parent / "fixtures/sea-client-graph-missing-bootstrap.json").read_text())
    fetch, requested = fetcher(manifest)
    with pytest.raises(AssertionError, match="required modules"):
        verify_client_bootstrap(html(manifest), fetch)
    assert requested == []


@pytest.mark.parametrize("fault", [
    "missing-batch", "missing-application", "missing-membership", "missing-html",
    "missing-script", "wrong-origin", "http-failure", "wrong-mime", "missing-registration",
])
def test_incomplete_client_bootstrap_cannot_pass(fault):
    manifest = graph()
    if fault == "missing-batch":
        manifest["batches"] = manifest["batches"][1:]
    if fault == "missing-application":
        manifest["batches"] = manifest["batches"][:1]
    if fault == "missing-membership":
        manifest["batches"][1]["entries"].remove(SETTINGS)
    if fault == "wrong-origin":
        manifest["batches"][0]["url"] = "https://example.invalid/bundle.js"
    fetch, _ = fetcher(
        manifest,
        status=404 if fault == "http-failure" else 200,
        content_type="text/html" if fault == "wrong-mime" else "text/javascript",
        omit=CORE if fault == "missing-registration" else None,
    )
    document = html(manifest, script=fault != "missing-script")
    if fault == "missing-html":
        document = document.replace("<html>", "")
    with pytest.raises(AssertionError):
        verify_client_bootstrap(document, fetch)
