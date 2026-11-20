from __future__ import annotations

from modiff.model import expand_strings


def test_a_string_with_newlines_becomes_an_indented_string() -> None:
    printed = '{\n  text = "one\\ntwo\\n";\n  name = "x";\n}'
    assert expand_strings(printed) == "{\n  text = ''\n    one\n    two\n  '';\n  name = \"x\";\n}"


def test_a_last_line_without_newline_closes_on_its_line() -> None:
    assert expand_strings('"a\\nb"') == "''\n  a\n  b''"


def test_escapes_become_indented_string_escapes() -> None:
    printed = '"q \\"x\\" \\${v} \'\'\\n\\\\"'
    assert expand_strings(printed) == "''\n  q \"x\" ''${v} '''\n  \\''"


def test_a_one_line_string_is_left_as_printed() -> None:
    printed = '{ "a.b" = "c\\td"; }'
    assert expand_strings(printed) == printed
