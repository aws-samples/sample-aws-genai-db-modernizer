# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each target engine's role in the architecture (owner, cache, search read model).

The rule lives in the facts layer (``src.shared.report_facts.engine_role``,
#615): synthesis records every engine's role in ``report.json``'s ``facts``,
and this module only re-exports the names the renderers already use.
"""

from __future__ import annotations

from src.shared.report_facts import CACHE_ENGINES as _CACHE_ENGINES
from src.shared.report_facts import RELATIONAL_ENGINES as _RELATIONAL_ENGINES
from src.shared.report_facts import SEARCH_ENGINES as _SEARCH_ENGINES
from src.shared.report_facts import SEARCH_READ_MODEL
from src.shared.report_facts import engine_role as _engine_role

__all__ = [
    "SEARCH_READ_MODEL",
    "_CACHE_ENGINES",
    "_RELATIONAL_ENGINES",
    "_SEARCH_ENGINES",
    "_engine_role",
]
