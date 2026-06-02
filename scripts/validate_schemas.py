#!/usr/bin/env python3
"""
Validate JSON schema files for Database Modernizer Assessment.

This script validates that JSON schema files:
1. Are valid JSON
2. Conform to JSON Schema Draft 07
3. Have required metadata fields (title, version, description)
"""

import json
import sys
from pathlib import Path

try:
    import jsonschema
    from jsonschema import Draft7Validator
except ImportError:
    print("Error: jsonschema package not installed")
    print("Install with: pip install jsonschema")
    sys.exit(1)


def validate_schema_file(file_path: Path) -> tuple[bool, list[str]]:
    """
    Validate a single JSON schema file.

    Args:
        file_path: Path to JSON schema file

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors = []

    try:
        # Load JSON file
        with open(file_path, encoding="utf-8") as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        errors.append(f"Invalid JSON: {e}")
        return False, errors
    except Exception as e:
        errors.append(f"Error reading file: {e}")
        return False, errors

    # Check if it's a JSON Schema
    if "$schema" not in schema:
        errors.append("Missing '$schema' field")
    elif "draft-07" not in schema["$schema"]:
        errors.append(f"Expected JSON Schema Draft 07, got: {schema['$schema']}")

    # Validate required metadata fields
    required_fields = ["title", "version", "description", "type"]
    for field in required_fields:
        if field not in schema:
            errors.append(f"Missing required field: '{field}'")

    # Validate version format (semantic versioning)
    if "version" in schema:
        version = schema["version"]
        parts = version.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            errors.append(f"Invalid version format: '{version}' (expected: MAJOR.MINOR.PATCH)")

    # Validate the schema itself using Draft7Validator
    try:
        Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        errors.append(f"Invalid JSON Schema: {e.message}")

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

        # Skip non-JSON files
        if file_path.suffix != ".json":
            continue

        # Skip files not in schemas directory
        if "schemas" not in file_path.parts:
            continue

        print(f"\nValidating: {file_path}")

        is_valid, errors = validate_schema_file(file_path)

        if is_valid:
            print("  ✓ Valid schema")
        else:
            print("  ✗ Validation failed:")
            for error in errors:
                print(f"    - {error}")
            all_valid = False

    if all_valid:
        print("\n✓ All schemas are valid")
        return 0
    else:
        print("\n✗ Some schemas have validation errors")
        return 1


if __name__ == "__main__":
    # Get file paths from command line arguments
    file_paths = sys.argv[1:]

    if not file_paths:
        # If no files provided, validate all schema files
        schemas_dir = Path(__file__).parent.parent / "docs" / "03-contracts" / "schemas"
        if schemas_dir.exists():
            file_paths = [str(f) for f in schemas_dir.glob("*.json")]
            if file_paths:
                print(f"No files specified, validating all schemas in {schemas_dir}")
            else:
                print(f"No JSON schema files found in {schemas_dir}")
                sys.exit(0)
        else:
            print(f"Schemas directory not found: {schemas_dir}")
            print("Skipping schema validation (no schemas to validate)")
            sys.exit(0)

    sys.exit(main(file_paths))
