"""YAML load/dump with round-trip preservation.

``ruamel.yaml`` in round-trip mode keeps comments, key order, and literal/
folded string styles intact. Editing someone's ``automations.yaml`` without
trashing their comments is non-negotiable; they should be able to diff
Mylo's changes against their old version and see only the structural
edits we actually made.

HA's magic constructors (``!secret``, ``!include``, ``!include_dir_list``,
etc.) would otherwise blow up a strict loader. We install them as
pass-through preserved tags so round-trip is safe even for configs that
reference them.
"""

from __future__ import annotations

import io
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.constructor import ConstructorError
from ruamel.yaml.nodes import ScalarNode, SequenceNode


class PreservedTag:
    """Stand-in for any HA-style ``!tag value`` scalar or sequence node.

    Holds the tag and the raw python value extracted from the node. On
    dump, we re-emit the original ``!tag`` form. Callers that need the
    underlying value (e.g. ``!secret wifi_password`` → ``wifi_password``)
    read :attr:`value`.
    """

    __slots__ = ("tag", "value")

    def __init__(self, tag: str, value: Any) -> None:
        self.tag = tag
        self.value = value

    def __repr__(self) -> str:
        return f"PreservedTag({self.tag!r}, {self.value!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, PreservedTag) and self.tag == other.tag and self.value == other.value
        )

    def __hash__(self) -> int:
        return hash((self.tag, repr(self.value)))


# HA uses these tags in configuration.yaml — recognize all common forms.
_HA_TAGS: tuple[str, ...] = (
    "!secret",
    "!include",
    "!include_dir_list",
    "!include_dir_merge_list",
    "!include_dir_named",
    "!include_dir_merge_named",
    "!env_var",
    "!input",
)


def _construct_preserved(loader: Any, tag_suffix: str, node: Any) -> PreservedTag:
    if isinstance(node, ScalarNode):
        return PreservedTag(tag_suffix, loader.construct_scalar(node))
    if isinstance(node, SequenceNode):
        return PreservedTag(tag_suffix, loader.construct_sequence(node))
    raise ConstructorError(
        None, None, f"unsupported node type for tag {tag_suffix!r}", node.start_mark
    )


def _represent_preserved(dumper: Any, data: PreservedTag) -> Any:
    if isinstance(data.value, list):
        return dumper.represent_sequence(data.tag, data.value)
    return dumper.represent_scalar(data.tag, str(data.value))


def _yaml_instance(*, typ: str = "rt") -> YAML:
    yaml = YAML(typ=typ)
    yaml.preserve_quotes = True
    # Reasonable defaults so dumps don't mangle style.
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096  # avoid surprise line-wraps inside strings

    for tag in _HA_TAGS:
        yaml.constructor.add_constructor(tag, _make_tagged_constructor(tag))
        yaml.representer.add_representer(PreservedTag, _represent_preserved)
    return yaml


def _make_tagged_constructor(tag: str) -> Any:
    def _ctor(loader: Any, node: Any) -> PreservedTag:
        return _construct_preserved(loader, tag, node)

    return _ctor


def load_yaml(text: str) -> Any:
    """Round-trip load. Returns whatever the document parses to."""
    yaml = _yaml_instance()
    return yaml.load(text) if text else None


def dump_yaml(data: Any) -> str:
    """Round-trip dump. Always ends with a newline."""
    yaml = _yaml_instance()
    buf = io.StringIO()
    yaml.dump(data, buf)
    out = buf.getvalue()
    if not out.endswith("\n"):
        out += "\n"
    return out


def safe_load(text: str) -> Any:
    """Non-preserving load for places that just need the Python value.

    Faster than round-trip. Still handles HA tags by returning
    :class:`PreservedTag` so downstream never crashes on a ``!secret``.
    """
    yaml = _yaml_instance(typ="safe")
    return yaml.load(text) if text else None
