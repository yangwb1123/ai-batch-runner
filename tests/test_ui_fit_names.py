"""Attribute routing and association regressions, independent of width metadata."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from ui_fit_jsx import accessible_name_missing, attribute, tag_end
from ui_fit_names import routing_from_markup
from ui_fit_registry import installed_vue_package, names_control_map

CONTROLS = ("IrisInput", "IrisSelect", "IrisSwitch", "IrisNumberInput")


def missing(source, fields=None):
    return accessible_name_missing(source, CONTROLS, {"IrisInput": False}, fields)


def test_names_registry_is_not_filtered_by_widths():
    assert names_control_map()["IrisInput"] is False
    assert names_control_map()["IrisCheckbox"] is True


def test_wrapper_marker_or_stepper_name_does_not_name_the_input():
    record = {"aria": '<div aria-label="UI_FIT_PROBE"><button aria-label="UI_FIT_PROBE">+</button><input></div>'}
    assert routing_from_markup(record)["aria"] is False
    assert routing_from_markup({"aria": '<input aria-label="UI_FIT_PROBE">'})["aria"] is True


def test_form_field_routing_requires_matching_native_id():
    assert routing_from_markup({"field": '<label data-iris-form-field-label for="a">Name</label><div id="a"><input id="b"></div>'})["form_field"] is False
    assert routing_from_markup({"field": '<label data-iris-form-field-label for="a">Name</label><input id="a">'})["form_field"] is True


def test_active_dependency_link_wins_over_stale_store(tmp_path):
    active = tmp_path / 'apps/web/node_modules/@iris-ui-kit/vue'
    stale = tmp_path / 'node_modules/.pnpm/@iris-ui-kit+vue@0.1.0/node_modules/@iris-ui-kit/vue'
    for package in (active, stale):
        (package / 'dist').mkdir(parents=True)
        (package / 'dist/index.js').write_text('export {}')
    assert installed_vue_package(tmp_path) == active.resolve()


def test_unrelated_label_does_not_exempt_an_input():
    source = '<label for="other">Other</label><IrisInput id="real" aria-label="Name" />'
    assert len(missing(source)) == 1
    assert not missing('<label for={id}>Name</label><IrisInput id={id} />')
    assert not missing('<label for="name">Name</label><IrisInput id={"name"} />')


def test_form_field_requires_verified_routing_and_a_label():
    source = '<IrisFormField label="Name"><IrisInput /></IrisFormField>'
    assert missing(source)
    assert not missing(source, {"IrisInput"})
    assert missing('<IrisFormField label=""><IrisInput /></IrisFormField>', {"IrisInput"})


def test_only_the_first_direct_component_child_is_cloned():
    fields = {"IrisInput"}
    assert missing('<IrisFormField label="Name"><div><IrisInput /></div></IrisFormField>', fields)
    assert missing('<IrisFormField label="Name"><IrisText /><IrisInput /></IrisFormField>', fields)
    source = '<IrisFormField label="Name"><IrisInput /><IrisInput /></IrisFormField>'
    assert len(missing(source, fields)) == 1


def test_comment_labels_and_attribute_urls_do_not_change_scope():
    assert missing('/* <label> */ <IrisInput /> // </label>')
    assert not missing('<label><span>https://example.test</span><IrisInput /></label>')
    assert attribute(' href="https://example.test" aria-label="Name"', 'aria-label') == 'literal:Name'


def test_arrow_and_comparison_are_not_attribute_ends():
    source = '<IrisInput onUpdate:modelValue={(n) => n > 0} aria-label="Name" />'
    assert tag_end(source, 0) == len(source)
    assert not accessible_name_missing(source, CONTROLS, {"IrisInput": True})
    assert missing('<IrisInput data-note="aria-label is absent" />')
    assert missing('<IrisInput aria-label="" />')


def test_tag_end_ignores_quoted_braces_and_escaped_quotes():
    sources = [
        r'''<IrisInput title="quoted \"value > }\"" value={{ nested: ">" }} />''',
        r'''<IrisInput title='quoted \'value > }\'' onChange={() => "}"} />''',
        '<IrisInput title={`value > }`} onChange={() => 1 > 0} />',
        '<IrisInput title="quoted > }" value={{ nested: { value: 1 } }} />',
    ]
    for source in sources:
        assert tag_end(source + '<span />', 0) == len(source)
