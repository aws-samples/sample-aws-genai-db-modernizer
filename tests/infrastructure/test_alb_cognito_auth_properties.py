"""
Regression tests for ALB Cognito authentication configuration.

Parses the CloudFormation template and verifies:
- Protected endpoints require Cognito authentication
- Health endpoint bypasses authentication

Runtime behavior is validated during integration testing after deployment.
"""

from pathlib import Path

import yaml  # type: ignore[import-untyped]


# Custom YAML loader that handles CloudFormation intrinsic functions
class CfnLoader(yaml.SafeLoader):
    pass


def _cfn_tag_constructor(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)


for tag in [
    "Ref",
    "Sub",
    "GetAtt",
    "Select",
    "Join",
    "Split",
    "If",
    "Equals",
    "And",
    "Or",
    "Not",
    "FindInMap",
    "Base64",
    "Cidr",
    "GetAZs",
    "ImportValue",
    "Transform",
    "Condition",
]:
    CfnLoader.add_constructor(
        f"!{tag}", lambda loader, node, t=tag: _cfn_tag_constructor(loader, t, node)
    )


API_SERVICE_TEMPLATE_PATH = Path("infrastructure/cloudformation/api-service.yaml")
with open(API_SERVICE_TEMPLATE_PATH, encoding="utf-8") as f:
    API_SERVICE_TEMPLATE = yaml.safe_load(
        f, Loader=CfnLoader
    )  # nosec B506 - CfnLoader extends SafeLoader

RESOURCES = API_SERVICE_TEMPLATE["Resources"]


class TestAlbCognitoAuthEnforcement:
    """Default listener requires Cognito auth. Validates: Requirements 11.4, 11.5"""

    def _get_listener_default_actions(self):
        return RESOURCES["AlbListener"]["Properties"]["DefaultActions"]

    def test_default_action_has_authenticate_cognito(self):
        actions = self._get_listener_default_actions()
        assert "authenticate-cognito" in [a["Type"] for a in actions]

    def test_authenticate_cognito_is_before_forward(self):
        actions = self._get_listener_default_actions()
        auth_order = next(a["Order"] for a in actions if a["Type"] == "authenticate-cognito")
        forward_order = next(a["Order"] for a in actions if a["Type"] == "forward")
        assert auth_order < forward_order

    def test_cognito_config_redirects_unauthenticated(self):
        actions = self._get_listener_default_actions()
        auth_action = next(a for a in actions if a["Type"] == "authenticate-cognito")
        assert (
            auth_action["AuthenticateCognitoConfig"]["OnUnauthenticatedRequest"] == "authenticate"
        )

    def test_cognito_config_has_required_scopes(self):
        actions = self._get_listener_default_actions()
        auth_action = next(a for a in actions if a["Type"] == "authenticate-cognito")
        scope = auth_action["AuthenticateCognitoConfig"]["Scope"]
        for s in ["openid", "email", "profile"]:
            assert s in scope

    def test_listener_uses_https(self):
        listener = RESOURCES["AlbListener"]["Properties"]
        assert listener["Protocol"] == "HTTPS"
        assert listener["Port"] == 443


class TestHealthEndpointAuthBypass:
    """Health endpoint forwards without auth. Validates: Requirements 11.8"""

    def test_health_rule_exists(self):
        assert "HealthCheckListenerRule" in RESOURCES

    def test_health_rule_matches_health_path(self):
        rule = RESOURCES["HealthCheckListenerRule"]
        conditions = rule["Properties"]["Conditions"]
        path_condition = next(c for c in conditions if c["Field"] == "path-pattern")
        assert "/health" in path_condition["Values"]

    def test_health_rule_has_highest_priority(self):
        rule = RESOURCES["HealthCheckListenerRule"]
        assert rule["Properties"]["Priority"] == 1

    def test_health_rule_forwards_without_auth(self):
        rule = RESOURCES["HealthCheckListenerRule"]
        actions = rule["Properties"]["Actions"]
        action_types = [a["Type"] for a in actions]
        assert "forward" in action_types
        assert "authenticate-cognito" not in action_types
