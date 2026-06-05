"""Abstract base classes for engine-specific load test components."""

from abc import ABC, abstractmethod
from typing import Any

from src.agents.load_test.models import RunResult, SeedManifest
from src.contracts.load_test_models import InfrastructureManifest, TestConfig


class BaseProvisioner(ABC):
    @abstractmethod
    def provision(self, schema_output: Any, tags: dict[str, str]) -> InfrastructureManifest:
        """Create all resources from engine-specific schema output."""

    @abstractmethod
    def teardown(self, manifest: InfrastructureManifest) -> None:
        """Delete all resources in the manifest."""


class BaseSeeder(ABC):
    @abstractmethod
    def seed(self, schema_output: Any, max_items_per_table: int) -> SeedManifest:
        """Seed data into provisioned infrastructure. Returns manifest with key ranges."""


class BaseScriptGenerator(ABC):
    @abstractmethod
    def generate_scenario(self, access_pattern: Any, table_definition: Any, seed_info: Any) -> str:
        """Generate one test script for one access pattern."""

    @abstractmethod
    def generate_main(self, scenarios: list, duration_minutes: int, warmup_seconds: int) -> str:
        """Generate the entry point that orchestrates all scenarios."""

    def generate_all(
        self,
        access_patterns: list,
        schema_output: Any,
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> str:
        """Generate all scripts to a temp directory. Returns scripts_dir path.
        Default implementation calls generate_scenario per pattern + generate_main.
        Subclasses may override for engine-specific needs."""
        raise NotImplementedError("Subclasses must implement generate_all")


class BaseRunner(ABC):
    @abstractmethod
    def run(self, scripts_dir: str, duration_minutes: int, env_vars: dict) -> RunResult:
        """Execute the full load test."""

    @abstractmethod
    def dry_run(self, scripts_dir: str, env_vars: dict) -> bool:
        """Validate scripts without running the full test."""
