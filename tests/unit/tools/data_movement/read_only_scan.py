# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A static scan of the AWS operations a module names (#1799, #1800).

Built from botocore's own service models (no network): every attribute and every string
constant in the module that is an operation name of the given services counts, whatever
object it is read from, so an alias (``rds = clients.rds``) or a ``getattr`` cannot hide
a write.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import botocore.session
from botocore import xform_name

READ_ONLY_PREFIXES = ("describe_", "get_", "list_")


@cache
def operation_names(services: tuple[str, ...]) -> frozenset[str]:
    session = botocore.session.get_session()
    return frozenset(
        xform_name(op) for s in services for op in session.get_service_model(s).operation_names
    )


def aws_operations(path: Path, services: tuple[str, ...]) -> list[tuple[int, str]]:
    """``(line, operation)`` for every operation name of ``services`` the module names."""
    ops = operation_names(services)
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Attribute) and node.attr in ops:
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in ops:
            found.append((node.lineno, node.value))
    return sorted(found)
