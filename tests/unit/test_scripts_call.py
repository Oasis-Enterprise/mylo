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

"""``--approve`` on the call.py CLI must grant the in-process wildcard,
not just flip ``user_approved`` — otherwise every tier-2/3 non-dry-run
call from the script gets refused by the executor's scoped-approval gate.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from mylo.scripts.call import _build_ctx
from tests.unit._helpers import make_config


def _ns(*, approve: bool, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(approve=approve, dry_run=dry_run)


def test_approve_grants_wildcard(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    ctx = _build_ctx(_ns(approve=True), client=object(), registries=object(), config=config)
    assert ctx.user_approved is True
    assert "*" in ctx.approved_plan_ids


def test_no_approve_grants_nothing(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    ctx = _build_ctx(_ns(approve=False), client=object(), registries=object(), config=config)
    assert ctx.user_approved is False
    assert ctx.approved_plan_ids == frozenset()
