"""Customer deliverables rendered from a synthesis report.

Pure functions of artifacts already on an ``ArtifactStore``: no LLM calls, no
platform calls. Used by the local scripts (``scripts/run_report.py``) and by the
AWS Transform integration, which adds publishing on top. Nothing in this package
may import ``src.atx_orchestrator``.
"""
