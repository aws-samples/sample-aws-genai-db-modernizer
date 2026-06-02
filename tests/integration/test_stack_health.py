"""
Integration tests that verify CloudFormation stack health.

These tests run against a live stack (feature branch or dev) and validate
that the stack deployed successfully with expected outputs.

The STACK_NAME environment variable must be set to the target stack name.
"""

import os

import boto3
import pytest

STACK_NAME = os.environ.get("STACK_NAME")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(scope="module")
def cfn_client():
    return boto3.client("cloudformation", region_name=AWS_REGION)


@pytest.fixture(scope="module")
def stack(cfn_client):
    if not STACK_NAME:
        pytest.skip("STACK_NAME not set — skipping integration tests")
    response = cfn_client.describe_stacks(StackName=STACK_NAME)
    assert len(response["Stacks"]) == 1, f"Stack {STACK_NAME} not found"
    return response["Stacks"][0]


@pytest.fixture(scope="module")
def stack_outputs(stack):
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


class TestStackHealth:
    """Verify the CloudFormation stack is in a healthy state."""

    def test_stack_status_is_complete(self, stack):
        assert (
            "COMPLETE" in stack["StackStatus"]
        ), f"Stack {STACK_NAME} is in unexpected state: {stack['StackStatus']}"

    def test_stack_has_outputs(self, stack):
        assert len(stack.get("Outputs", [])) > 0, f"Stack {STACK_NAME} has no outputs"

    def test_vpc_output_exists(self, stack_outputs):
        assert "VpcId" in stack_outputs, "Stack missing VpcId output"

    def test_vpc_id_format(self, stack_outputs):
        vpc_id = stack_outputs.get("VpcId", "")
        assert vpc_id.startswith("vpc-"), f"VpcId has unexpected format: {vpc_id}"
