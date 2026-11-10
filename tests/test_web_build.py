"""Release checks must reject stale/missing inputs, not just validate old bundles."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest

from pbatch.hub_web_assets import bundle
from pbatch import hub_web_provenance as provenance

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source(tmp_path):
    target = tmp_path / 'web'
    target.mkdir()
    for name in provenance.INPUT_FILES:
        shutil.copyfile(ROOT / 'web' / name, target / name)
    for name in provenance.INPUT_DIRS:
        shutil.copytree(ROOT / 'web' / name, target / name)
    return target


def test_shipped_bundle_matches_current_sources_without_bun_or_iris_checkout():
    manifest, _ = bundle()
    provenance.check_sources(ROOT / 'web', manifest)
    assert manifest['build']['bun'] == '1.4.2'


@pytest.mark.parametrize('change', ['edit', 'add', 'remove', 'lock', 'iris', 'toolchain'])
def test_stale_build_is_not_accepted(source, change):
    manifest, _ = bundle()
    if change == 'edit':
        (source / 'src/App.svelte').write_text('different application')
    elif change == 'add':
        (source / 'src/new.ts').write_text('export const newSource = true')
    elif change == 'remove':
        (source / 'src/main.ts').unlink()
    elif change == 'lock':
        (source / 'bun.lock').write_text('{}')
    elif change == 'iris':
        (source / 'iris-source.json').write_text('{}')
    else:
        manifest['build']['bun'] = '0.0.0'
    with pytest.raises(ValueError):
        provenance.check_sources(source, manifest)


@pytest.mark.parametrize('kind', ['file-link', 'directory-link', 'oversize', 'too-many'])
def test_source_collection_rejects_unsafe_or_unbounded_inputs(source, kind):
    if kind == 'file-link':
        (source / 'src/link.ts').symlink_to(source / 'src/main.ts')
    elif kind == 'directory-link':
        (source / 'src/alias').symlink_to(source / 'src', target_is_directory=True)
    elif kind == 'oversize':
        (source / 'src/huge.ts').write_bytes(b'x' * (provenance.MAX_FILE_BYTES + 1))
    else:
        for number in range(provenance.MAX_FILES):
            (source / f'src/extra{number}.ts').write_text('export {}')
    with pytest.raises((ValueError, OSError)):
        provenance.source_files(source)


@pytest.mark.parametrize('change', ['field', 'path', 'bool', 'hash', 'bun', 'missing'])
def test_malformed_build_provenance_fails_closed(change):
    manifest, _ = bundle()
    build = deepcopy(manifest['build'])
    name = 'src/main.ts'
    if change == 'field':
        build['approved'] = True
    elif change == 'path':
        build['sources']['../outside.ts'] = build['sources'].pop(name)
    elif change == 'bool':
        build['sources'][name]['bytes'] = True
    elif change == 'hash':
        build['sources'][name]['sha256'] = 'unknown'
    elif change == 'bun':
        build['bun'] = 1
    else:
        build['sources'].pop('package.json')
    with pytest.raises(ValueError):
        provenance.validate_build(build)


def test_docs_are_not_compiler_inputs_but_scripts_and_tests_are(source):
    expected = provenance.source_files(source)
    (source / 'README.md').write_text('documentation only')
    assert provenance.source_files(source) == expected
    assert 'scripts/assets.ts' in expected and 'tests/workspace.test.ts' in expected
    assert '.iris' not in expected and 'node_modules' not in expected


def test_release_targets_require_source_check_and_exact_rebuild():
    make = (ROOT / 'Makefile').read_text()
    assert 'web-assets-check' in next(line for line in make.splitlines() if line.startswith('ci:'))
    assert 'python tools/check-web-build.py' in make
    assert 'tests/browser_hub_web_control.py' in make
    assert 'tests/browser_hub_web_results.py' in make
    assert 'tests/browser_hub_web_catalog.py' in make
    assert 'bun run build:check' in make
    scripts = json.loads((ROOT / 'web/package.json').read_text())['scripts']
    assert scripts['build:check'] == 'bun scripts/build.ts --check'


def test_ci_and_both_release_paths_require_pinned_web_validation():
    import yaml
    action = yaml.safe_load((ROOT / '.github/actions/verify-web/action.yml').read_text())
    steps = action['runs']['steps']
    iris = json.loads((ROOT / 'web/iris-source.json').read_text())
    checkout = next(step for step in steps if step.get('name') == 'Read-only reviewed Iris source')
    assert checkout['with']['ref'] == iris['revision']
    assert 'https://github.com/' + checkout['with']['repository'] + '.git' == iris['repository']
    assert checkout['with']['persist-credentials'] is False
    assert steps[0]['with']['bun-version'] == '1.4.2'
    commands = '\n'.join(step.get('run', '') for step in steps)
    assert '--frozen-lockfile --ignore-scripts' in commands
    assert 'make web-check' in commands and 'make web-browser' in commands
    for file, job in [('ci.yml', 'web'), ('release.yml', 'build-and-verify'), ('release-binary.yml', 'validate')]:
        data = yaml.safe_load((ROOT / '.github/workflows' / file).read_text())
        step = next(item for item in data['jobs'][job]['steps']
                    if item.get('uses') == './.github/actions/verify-web')
        assert 'if' not in step and 'continue-on-error' not in step
        assert 0 < step['timeout-minutes'] <= 8
