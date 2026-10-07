"""Native name evidence from the active installed Vue artifact, not its version.

SSR only establishes attribute/label routing, never browser layout or hydration.
The bounded probe executes installed component code like a project test does;
missing tools/exports remain explicitly unverified and do not grant exemptions.
"""
from __future__ import annotations

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from ui_fit_widths import floors_from_markup

PROBE = r"""
const { createRequire } = require('node:module');
const requirePackage = createRequire(process.argv[1] + '/package.json');
const { createSSRApp, h } = requirePackage('vue');
const { renderToString } = requirePackage('vue/server-renderer');
const iris = requirePackage('@iris-ui-kit/vue');
const names = JSON.parse(process.argv[2]);
const { lightTheme } = requirePackage('@iris-ui-kit/tokens');
const spacing = Object.fromEntries(Object.entries(lightTheme.spacing).map(([key,value]) =>
  ['--' + key.replaceAll('.', '-'), String(value) + 'px']));
(async () => {
  const result = {};
  for (const name of names) {
    if (!iris[name]) continue;
    try {
      const render = (node) => renderToString(createSSRApp({ render: () => node }));
      const props = { items: [], modelValue: undefined };
      const aria = await render(h(iris[name], { ...props, 'aria-label': 'UI_FIT_PROBE' }));
      const camel = await render(h(iris[name], { ...props, ariaLabel: 'UI_FIT_PROBE' }));
      const field = iris.IrisFormField ? await render(h(iris.IrisFormField,
        { label: 'UI_FIT_FIELD' }, { default: () => h(iris[name], props) })) : '';
      const sizes = {};
      for (const size of ['sm', 'md', 'lg']) sizes[size] = await render(h(iris[name], { ...props, size }));
      result[name] = { aria, camel, field, sizes };
    } catch (error) { result[name] = { error: String(error) }; }
  }
  process.stdout.write(JSON.stringify({ records: result, spacing }));
})().catch(() => process.exit(1));
"""


class NameMarkup(HTMLParser):
    """Keep labelable controls, avoiding named stepper buttons or host wrappers."""

    def __init__(self, markup: str):
        super().__init__(convert_charrefs=True)
        self.controls: list[dict[str, str]] = []
        self.labels: set[str] = set()
        self.feed(markup)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "label" and attributes.get("for"):
            if "data-iris-form-field-label" in attributes:
                self.labels.add(attributes["for"])
        native = tag in {"input", "select", "textarea"}
        trigger = tag == "button" and "aria-haspopup" in attributes
        if (native and attributes.get("type") != "hidden") or trigger:
            self.controls.append(attributes)


def routing_from_markup(record: dict) -> dict[str, bool]:
    """The exact probe marker must name the native element, not another node."""
    routing = {}
    for key in ("aria", "camel"):
        markup = NameMarkup(record.get(key, ""))
        routing[key] = any(c.get("aria-label") == "UI_FIT_PROBE" for c in markup.controls)
    field = NameMarkup(record.get("field", ""))
    routing["form_field"] = any(c.get("id") in field.labels for c in field.controls)
    return routing


def _probe_evidence(payload: dict, package: Path, controls: tuple[str, ...]) -> dict:
    records = payload["records"]
    verified = {name: routing_from_markup(data) for name, data in records.items()
                if name in controls and isinstance(data, dict) and "error" not in data}
    return {"source": "installed-ssr", "controls": verified,
            "widths_by_size": floors_from_markup(records, payload["spacing"]),
            "width_basis": "whole-control SSR minimum under canonical light spacing tokens",
            "cell_padding": 2 * int(payload["spacing"]["--iris-padding-md"].removesuffix("px")),
            "package": str(package), "unverified": sorted(set(controls) - set(verified))}


def installed_name_routing(package: Path | None, controls: tuple[str, ...]) -> dict:
    """Probe once per CLI invocation, with timeout and bounded retained output."""
    if package is None:
        return {"source": "unverified", "controls": {}, "reason": "Vue package not resolved"}
    try:
        # Probe HTML is bounded by the small fixed component set; communicate
        # through temp files so noisy package diagnostics are not buffered.
        import tempfile
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            run = subprocess.run(
                ["node", "-e", PROBE, str(package), json.dumps(controls)],
                stdout=out, stderr=err, timeout=20, check=False,
            )
            out.seek(0)
            raw = out.read(512 * 1024 + 1)
        if run.returncode or len(raw) > 512 * 1024:
            raise ValueError("probe failed or exceeded its output budget")
        return _probe_evidence(json.loads(raw), package, controls)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as exc:
        return {"source": "unverified", "controls": {}, "reason": str(exc)}
