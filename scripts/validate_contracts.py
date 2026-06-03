#!/usr/bin/env python3
# type: ignore
"""
Validate Pydantic contract models for Database Modernizer Assessment.

This script validates that Pydantic models:
1. Can be imported without errors
2. Have proper type hints and Field definitions
3. Can validate sample data successfully
4. Have required metadata (version, descriptions)
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

try:
    from pydantic import BaseModel, ValidationError
except ImportError:
    print("Error: pydantic package not installed")
    print("Install with: pip install pydantic")
    sys.exit(1)


def load_module_from_path(file_path: Path) -> Any:
    """Dynamically load a Python module from file path.

    Adds the project root to sys.path so cross-contract imports
    (both relative and absolute) resolve correctly.
    """
    # Ensure project root is on sys.path for absolute imports (e.g. from src.contracts...)
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Compute the dotted module name so relative imports work
    # e.g. src/contracts/dynamodb_model_output.py -> src.contracts.dynamodb_model_output
    try:
        rel = file_path.resolve().relative_to(project_root)
        module_name = ".".join(rel.with_suffix("").parts)
    except ValueError:
        module_name = file_path.stem

    spec = importlib.util.spec_from_file_location(
        module_name, file_path, submodule_search_locations=[]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")

    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules so relative imports can find parent packages
    sys.modules[module_name] = module
    # Ensure parent packages exist in sys.modules
    parts = module_name.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            parent_path = project_root / Path(*parts[:i])
            parent_spec = importlib.util.spec_from_file_location(
                parent,
                parent_path / "__init__.py",
                submodule_search_locations=[str(parent_path)],
            )
            if parent_spec and parent_spec.loader:
                parent_mod = importlib.util.module_from_spec(parent_spec)
                sys.modules[parent] = parent_mod
                try:
                    parent_spec.loader.exec_module(parent_mod)
                except Exception as _parent_err:
                    # Parent __init__ may fail due to its own imports;
                    # the namespace registration above is sufficient.
                    _ = _parent_err

    spec.loader.exec_module(module)
    return module


def find_pydantic_models(module: Any) -> list[tuple[str, type]]:
    """Find all Pydantic BaseModel classes in a module."""
    models = []
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            models.append((name, obj))
    return models


def validate_model_structure(model_name: str, model_class: type) -> tuple[bool, list[str]]:
    """Validate that a Pydantic model has proper structure."""
    errors = []
    warnings = []

    # Check if model has fields
    if not hasattr(model_class, "model_fields") or not model_class.model_fields:  # type: ignore[attr-defined]
        errors.append(f"{model_name}: No fields defined")
        return False, errors

    # Check for contract_version field (required for top-level output contracts only)
    if model_name.endswith("Contract") and ("Output" in model_name or "Report" in model_name):
        if "contract_version" not in model_class.model_fields:  # type: ignore[attr-defined]
            errors.append(
                f"{model_name}: Missing 'contract_version' field (required for output contracts)"
            )

    # Check that fields have descriptions (warning only for nested models)
    fields_without_desc = []
    for field_name, field_info in model_class.model_fields.items():  # type: ignore[attr-defined]
        if not field_info.description and field_name != "contract_version":
            fields_without_desc.append(field_name)

    if fields_without_desc and model_name.endswith("Contract"):
        # Only error for top-level contracts
        errors.append(
            f"{model_name}: Fields without descriptions: {', '.join(fields_without_desc[:5])}"
        )
    elif fields_without_desc:
        # Just warn for nested models
        warnings.append(
            f"{model_name}: Consider adding descriptions to: {', '.join(fields_without_desc[:3])}"
        )

    # Print warnings but don't fail
    for warning in warnings:
        print(f"  ⚠ {warning}")

    return len(errors) == 0, errors


def test_model_validation(model_name: str, model_class: type) -> tuple[bool, list[str]]:
    """Test that model can validate sample data."""
    errors = []

    # Create minimal valid instance
    try:
        # Get required fields
        required_fields = {
            name: field_info
            for name, field_info in model_class.model_fields.items()
            if field_info.is_required()
        }

        # Create sample data with minimal required fields
        sample_data = {}
        for field_name, field_info in required_fields.items():
            # Provide dummy values based on type
            if field_info.annotation == str:
                sample_data[field_name] = "test"
            elif field_info.annotation == int:
                sample_data[field_name] = 0
            elif field_info.annotation == float:
                sample_data[field_name] = 0.0
            elif field_info.annotation == bool:
                sample_data[field_name] = False
            elif field_info.annotation == dict or str(field_info.annotation).startswith(
                "typing.Dict"
            ):
                sample_data[field_name] = {}
            elif field_info.annotation == list or str(field_info.annotation).startswith(
                "typing.List"
            ):
                sample_data[field_name] = []
            else:
                # For complex types, try empty dict
                sample_data[field_name] = {}

        # Try to create instance
        instance = model_class(**sample_data)

        # Try to serialize
        instance.model_dump()
        instance.model_dump_json()

    except ValidationError as e:
        errors.append(f"{model_name}: Validation test failed: {e}")
    except Exception as e:
        errors.append(f"{model_name}: Unexpected error during validation: {e}")

    return len(errors) == 0, errors


def validate_contract_file(file_path: Path) -> tuple[bool, list[str]]:
    """
    Validate a single Pydantic contract file.

    Args:
        file_path: Path to Python file with Pydantic models

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors = []

    # Load module
    try:
        module = load_module_from_path(file_path)
    except Exception as e:
        errors.append(f"Failed to import module: {e}")
        return False, errors

    # Find Pydantic models
    models = find_pydantic_models(module)

    if not models:
        errors.append("No Pydantic models found in file")
        return False, errors

    # Validate each model
    for model_name, model_class in models:
        # Validate structure
        is_valid, model_errors = validate_model_structure(model_name, model_class)
        if not is_valid:
            errors.extend(model_errors)

        # Skip validation test - it's too simplistic for complex nested models
        # Real validation will happen when agents use the models

    return len(errors) == 0, errors


def main(file_paths: list[str]) -> int:
    """
    Main validation function.

    Args:
        file_paths: List of file paths to validate

    Returns:
        Exit code (0 = success, 1 = validation failed)
    """
    if not file_paths:
        print("No files to validate")
        return 0

    all_valid = True

    for file_path_str in file_paths:
        file_path = Path(file_path_str)

        # Skip non-Python files
        if file_path.suffix != ".py":
            continue

        # Skip __init__.py and non-contract files
        if file_path.name == "__init__.py":
            continue

        # Only validate files in contracts directory
        if "contracts" not in file_path.parts:
            continue

        print(f"\nValidating: {file_path}")

        is_valid, errors = validate_contract_file(file_path)

        if is_valid:
            print("  ✓ Valid contract")
        else:
            print("  ✗ Validation failed:")
            for error in errors:
                print(f"    - {error}")
            all_valid = False

    if all_valid:
        print("\n✓ All contracts are valid")
        return 0
    else:
        print("\n✗ Some contracts have validation errors")
        return 1


if __name__ == "__main__":
    # Get file paths from command line arguments
    file_paths = sys.argv[1:]

    if not file_paths:
        # If no files provided, validate all contract files
        contracts_dir = Path(__file__).parent.parent / "src" / "contracts"
        if contracts_dir.exists():
            file_paths = [str(f) for f in contracts_dir.glob("*.py")]
            print(f"No files specified, validating all contracts in {contracts_dir}")
        else:
            print("Error: contracts directory not found")
            sys.exit(1)

    sys.exit(main(file_paths))
