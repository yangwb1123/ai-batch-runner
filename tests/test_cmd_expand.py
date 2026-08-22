"""expand_cmd placeholder expansion regression tests (single entry point)."""

from __future__ import annotations

from pbatch.cmd_expand import expand_cmd


def test_files_placeholder_renders_quoted_paths():
    out = expand_cmd("run --files {files}", None, "/proj",
                     files=["src/a.py", "src/b b.py"])
    assert out == "run --files src/a.py 'src/b b.py'"


def test_files_placeholder_unset_preserves_literal():
    assert expand_cmd("run --files {files}", None, "/proj") == \
        "run --files {files}"


def test_files_combines_with_output_and_cwd():
    out = expand_cmd("check {files} -o {output} in {cwd}", "out.json", "/proj",
                     files=["a.py"])
    assert out == "check a.py -o out.json in /proj"