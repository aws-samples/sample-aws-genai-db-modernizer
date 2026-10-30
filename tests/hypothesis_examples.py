# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Example budgets that follow the loaded Hypothesis profile (#956).

tests/conftest.py loads the ``default`` profile (100 examples) unless
``HYPOTHESIS_PROFILE`` names another; ``ci`` (20 examples) is the one
ci/test.sh selects on pull requests. A test that sets its own
``max_examples`` would ignore the profile, so it asks for
``scaled(n)`` instead: ``n`` under ``default``, and the same share of
``n`` that the loaded profile takes of 100 otherwise.
"""

from hypothesis import settings

_DEFAULT_EXAMPLES = 100


def scaled(n: int) -> int:
    loaded: int = settings().max_examples
    return max(1, n * loaded // _DEFAULT_EXAMPLES)
