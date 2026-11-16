# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR invariants checked as queries over a job's saved artifacts (#2143).

``load.load_job`` reads a job folder once into an in-process DuckDB database (one
table per fact the checks read); ``checks.run_checks`` runs each invariant as one SQL
query over it. The queries are oracles: they check what the pipeline saved and decide
nothing. ``scripts/check_invariants.py`` is the command line.
"""
