"""Tests for the join key in :mod:`graphcode.keys`."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from graphcode.keys import (
    InvalidKeyError,
    NodeKey,
    is_inside_package,
    module_name_from_path,
)


class _Outer:
    """Class whose methods produce real nested qualified names."""

    def method(self) -> Callable[[], Callable[[], int]]:
        """Return a nested function that itself returns a lambda."""

        def inner() -> Callable[[], int]:
            return lambda: 0

        return inner


_NESTED_FUNCTION = _Outer().method()


@pytest.mark.parametrize(
    "python_object",
    [_Outer, _Outer.method, _NESTED_FUNCTION, _NESTED_FUNCTION()],
    ids=["class", "method", "nested function", "lambda in nested function"],
)
def test_real_qualnames_round_trip(python_object: object) -> None:
    """Qualified names produced by Python survive to_string / from_string."""
    key = NodeKey("pkg.mod", python_object.__qualname__)
    assert NodeKey.from_string(key.to_string()) == key


def test_child_rebuilds_python_qualname() -> None:
    """child() inserts <locals> exactly where Python does."""
    module_key = NodeKey("pkg.mod")
    built = module_key.child("_Outer").child("method").child("inner", inside_function=True)
    assert built.qualname == _NESTED_FUNCTION.__qualname__


def test_module_key_has_no_colon() -> None:
    """A module key is its dotted path alone."""
    key = NodeKey("datagenerator.dag")
    assert key.is_module
    assert key.to_string() == "datagenerator.dag"
    assert NodeKey.from_string("datagenerator.dag") == key


@pytest.mark.parametrize("text", ["pkg:Δt", "élan:π", "pkg:f.<locals>.<genexpr>"])
def test_valid_keys_accepted(text: str) -> None:
    """Non-ASCII identifiers and generated names are valid."""
    assert NodeKey.from_string(text).to_string() == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "pkg.mod:",
        "pkg..mod",
        "1pkg",
        "pkg.mod:a b",
        "pkg.mod:func\n",
        "pkg\n",
        "pkg:<module>",
        "pkg:<locals>",
        "pkg:a²",
        "pkg:<generic parameters of f>",
        "a:b:c",
    ],
)
def test_invalid_keys_rejected(text: str) -> None:
    """Malformed keys raise instead of silently never matching."""
    with pytest.raises(InvalidKeyError):
        NodeKey.from_string(text)


@pytest.mark.parametrize(
    ("key", "expected_parent"),
    [
        ("pkg.mod:DAG.add_node", "pkg.mod:DAG"),
        ("pkg.mod:f.<locals>.inner", "pkg.mod:f"),
        ("pkg.mod:DAG", "pkg.mod"),
        ("pkg.mod", "pkg"),
        ("pkg", None),
    ],
)
def test_parent_key(key: str, expected_parent: str | None) -> None:
    """parent_key() removes exactly one level, skipping <locals>."""
    parent = NodeKey.from_string(key).parent_key()
    assert (None if parent is None else str(parent)) == expected_parent


def test_parent_key_inverts_child() -> None:
    """Going down with child() and back up with parent_key() is a no-op."""
    function_key = NodeKey("pkg.mod", "f")
    assert function_key.child("inner", inside_function=True).parent_key() == function_key


def test_child_rejects_dotted_name() -> None:
    """child() takes one identifier, not a dotted path."""
    with pytest.raises(InvalidKeyError):
        NodeKey("pkg").child("a.b")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("pkg/dag.py", "pkg.dag"),
        ("pkg/sub/__init__.py", "pkg.sub"),
        ("pkg/été.py", "pkg.été"),
    ],
)
def test_module_name_from_path(path: str, expected: str) -> None:
    """File paths relative to the source root map to dotted module names."""
    assert module_name_from_path(path) == expected


@pytest.mark.parametrize("path", ["__init__.py", "/abs/pkg/mod.py", "pkg/data.json", "pkg/my-mod.py"])
def test_module_name_from_invalid_path(path: str) -> None:
    """Paths that cannot be imported as modules are rejected."""
    with pytest.raises(InvalidKeyError):
        module_name_from_path(path)


def test_is_inside_package() -> None:
    """The package itself and its submodules are inside; look-alikes are not."""
    assert is_inside_package("datagenerator", "datagenerator")
    assert is_inside_package("datagenerator.dag", "datagenerator")
    assert not is_inside_package("datagenerator_extra", "datagenerator")
