# Copyright 2026 Maxwell Monson / Oasis Enterprise LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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

import copy
import io
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.constructor import ConstructorError
from ruamel.yaml.nodes import ScalarNode, SequenceNode
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

# Patterns that YAML 1.1 (which HA's PyYAML parser uses) would
# *mis-interpret* as non-strings when emitted without quotes:
#   - 23:00:00 → sexagesimal integer
#   - true/false/yes/no/on/off → boolean
#   - null/~ → null
#   - numeric-looking → int or float
#
# We force double-quotes on anything matching these so the round-tripped
# YAML still means the same thing when HA reads it. Targeted, so normal
# text values like aliases stay unquoted and readable.
_YAML_AMBIGUOUS = re.compile(
    r"""^(?:
        # Time-like: HH:MM or HH:MM:SS
        \d{1,2}:\d{2}(?::\d{2})?
      | # YAML 1.1 booleans
        (?:true|false|yes|no|on|off|y|n)
      | # Null tokens
        (?:null|~)
      | # Numbers (int, float, hex, octal)
        [+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?
      | # Leading zero forms YAML 1.1 treated specially
        0x[0-9a-fA-F]+|0o?[0-7]+
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


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
    """Round-trip dump. Always ends with a newline.

    Walks the structure first to wrap YAML-1.1-ambiguous strings in
    :class:`DoubleQuotedScalarString`, so values like ``"23:00:00"`` land
    in the output as ``"23:00:00"`` rather than the bare ``23:00:00``
    that HA's parser would read as sexagesimal 82800.
    """
    yaml = _yaml_instance()
    buf = io.StringIO()
    yaml.dump(_quote_ambiguous(data), buf)
    out = buf.getvalue()
    if not out.endswith("\n"):
        out += "\n"
    return out


def _quote_ambiguous(value: Any) -> Any:
    """Return a copy of ``value`` with YAML-1.1-ambiguous leaf strings
    wrapped in :class:`DoubleQuotedScalarString`.

    We deep-copy then mutate in place so ruamel's ``CommentedMap`` /
    ``CommentedSeq`` keep their comment + key-order metadata. Rebuilding
    them with ``type(value)(...)`` strips those anchors — that broke the
    round-trip-preserves-comments contract.
    """
    cloned = copy.deepcopy(value)
    _quote_in_place(cloned)
    return cloned


def _quote_in_place(value: Any) -> None:
    if isinstance(value, dict):
        for k, v in list(value.items()):
            if isinstance(v, str) and not isinstance(v, DoubleQuotedScalarString):
                if _YAML_AMBIGUOUS.match(v):
                    value[k] = DoubleQuotedScalarString(v)
            else:
                _quote_in_place(v)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            if isinstance(v, str) and not isinstance(v, DoubleQuotedScalarString):
                if _YAML_AMBIGUOUS.match(v):
                    value[i] = DoubleQuotedScalarString(v)
            else:
                _quote_in_place(v)


def safe_load(text: str) -> Any:
    """Non-preserving load for places that just need the Python value.

    Faster than round-trip. Still handles HA tags by returning
    :class:`PreservedTag` so downstream never crashes on a ``!secret``.
    """
    yaml = _yaml_instance(typ="safe")
    return yaml.load(text) if text else None
