"""Verify abstract base classes cannot be instantiated and define correct interface."""
import pytest

from src.agents.load_test.base import BaseProvisioner, BaseRunner, BaseScriptGenerator, BaseSeeder


def test_base_provisioner_is_abstract():
    with pytest.raises(TypeError):
        BaseProvisioner()


def test_base_seeder_is_abstract():
    with pytest.raises(TypeError):
        BaseSeeder()


def test_base_script_generator_is_abstract():
    with pytest.raises(TypeError):
        BaseScriptGenerator()


def test_base_runner_is_abstract():
    with pytest.raises(TypeError):
        BaseRunner()
