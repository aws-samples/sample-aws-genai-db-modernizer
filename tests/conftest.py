"""Root test configuration with Hypothesis profiles."""

import os

from hypothesis import settings

# CI profile: fewer examples for faster pipeline runs
settings.register_profile("ci", max_examples=20, deadline=None)

# Default: full 100 examples for local development
settings.register_profile("default", max_examples=100, deadline=None)

settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))
