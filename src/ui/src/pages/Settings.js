import React, { useState } from "react";
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Form from "@cloudscape-design/components/form";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Header from "@cloudscape-design/components/header";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Select from "@cloudscape-design/components/select";
import Input from "@cloudscape-design/components/input";
import Slider from "@cloudscape-design/components/slider";
import Checkbox from "@cloudscape-design/components/checkbox";
import RadioGroup from "@cloudscape-design/components/radio-group";
import Toggle from "@cloudscape-design/components/toggle";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import { useTranslation } from "react-i18next";
import { SideNavigationConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
function SettingsPage() {
  const { t } = useTranslation();
  // AWS Configuration State
  const [awsRegion, setAwsRegion] = useState({
    description: "us-east-1",
    label: "US East (N. Virginia)",
    value: "us-east-1",
  });
  const [s3Bucket, setS3Bucket] = useState(
    "database-modernizer-results-us-east-1",
  );
  const [dynamoDbTable, setDynamoDbTable] = useState(
    "database-modernizer-jobs",
  );
  const [iamRole, setIamRole] = useState("");

  // Default Analysis Options State
  const [queryLogPeriod, setQueryLogPeriod] = useState(7);
  const [sampleSize, setSampleSize] = useState(1000);
  const [targetDatabases, setTargetDatabases] = useState({
    dynamodb: true,
    documentdb: true,
    elasticache: true,
    opensearch: true,
    neptune: false,
    keyspaces: false,
    aurora: true,
  });
  const [piiAnonymization, setPiiAnonymization] = useState(true);
  const [sampleDataCollection, setSampleDataCollection] = useState(true);

  // UI Preferences State
  const [colorTheme, setColorTheme] = useState("system");
  const [autoRefreshInterval, setAutoRefreshInterval] = useState({
    label: "Every 30 seconds",
    value: "30",
  });
  const [browserNotifications, setBrowserNotifications] = useState(true);
  const [emailNotifications, setEmailNotifications] = useState(false);
  const [notificationEvents, setNotificationEvents] = useState({
    completed: true,
    failed: true,
    warnings: false,
    longRunning: false,
  });
  const [compactMode, setCompactMode] = useState(false);

  // Validation Error State
  const [errors, setErrors] = useState({
    s3Bucket: "",
    dynamoDbTable: "",
    iamRole: "",
  });
  const regionOptions = [
    {
      description: "us-east-1",
      label: "US East (N. Virginia)",
      value: "us-east-1",
    },
    {
      description: "us-east-2",
      label: "US East (Ohio)",
      value: "us-east-2",
    },
    {
      description: "us-west-1",
      label: "US West (N. California)",
      value: "us-west-1",
    },
    {
      description: "us-west-2",
      label: "US West (Oregon)",
      value: "us-west-2",
    },
    {
      description: "eu-west-1",
      label: "Europe (Ireland)",
      value: "eu-west-1",
    },
    {
      description: "eu-west-2",
      label: "Europe (London)",
      value: "eu-west-2",
    },
    {
      description: "eu-central-1",
      label: "Europe (Frankfurt)",
      value: "eu-central-1",
    },
    {
      description: "ap-northeast-1",
      label: "Asia Pacific (Tokyo)",
      value: "ap-northeast-1",
    },
    {
      description: "ap-southeast-1",
      label: "Asia Pacific (Singapore)",
      value: "ap-southeast-1",
    },
    {
      description: "ap-southeast-2",
      label: "Asia Pacific (Sydney)",
      value: "ap-southeast-2",
    },
  ];
  const refreshIntervalOptions = [
    {
      description: "Manual refresh only",
      label: "Disabled",
      value: "disabled",
    },
    {
      description: "High frequency updates",
      label: "Every 5 seconds",
      value: "5",
    },
    {
      description: "Frequent updates",
      label: "Every 10 seconds",
      value: "10",
    },
    {
      description: "Balanced updates (recommended)",
      label: "Every 30 seconds",
      tags: ["Recommended"],
      value: "30",
    },
    {
      description: "Moderate updates",
      label: "Every 1 minute",
      value: "60",
    },
    {
      description: "Low frequency updates",
      label: "Every 5 minutes",
      value: "300",
    },
  ];
  const themeOptions = [
    {
      description:
        "Automatically match your operating system's theme settings.",
      label: "System default",
      value: "system",
    },
    {
      description:
        "Use light colors for the interface background and elements.",
      label: "Light theme",
      value: "light",
    },
    {
      description: "Use dark colors for the interface background and elements.",
      label: "Dark theme",
      value: "dark",
    },
  ];

  // Validation Functions
  const validateS3Bucket = (value) => {
    if (!value) {
      return "S3 bucket name is required";
    }
    if (value.length < 3 || value.length > 63) {
      return "Bucket name must be between 3 and 63 characters long";
    }
    if (!/^[a-z0-9][a-z0-9.-]*[a-z0-9]$/.test(value)) {
      return "Bucket name can only contain lowercase letters, numbers, hyphens, and periods";
    }
    if (/\.\./.test(value) || /--/.test(value)) {
      return "Bucket name cannot contain consecutive periods or hyphens";
    }
    return "";
  };
  const validateDynamoDbTable = (value) => {
    if (!value) {
      return "DynamoDB table name is required";
    }
    if (value.length < 3 || value.length > 255) {
      return "Table name must be between 3 and 255 characters long";
    }
    if (!/^[a-zA-Z0-9_.-]+$/.test(value)) {
      return "Table name can only contain letters, numbers, underscores, hyphens, and periods";
    }
    return "";
  };
  const validateIamRole = (value) => {
    if (!value) {
      return ""; // Optional field
    }
    if (!/^arn:aws:iam::\d{12}:role\/[\w+=,.@-]+$/.test(value)) {
      return "IAM role ARN must be in the format: arn:aws:iam::account-id:role/role-name";
    }
    return "";
  };

  // Event Handlers
  const handleS3BucketChange = ({ detail }) => {
    setS3Bucket(detail.value);
    setErrors((prev) => ({
      ...prev,
      s3Bucket: "",
    }));
  };
  const handleDynamoDbTableChange = ({ detail }) => {
    setDynamoDbTable(detail.value);
    setErrors((prev) => ({
      ...prev,
      dynamoDbTable: "",
    }));
  };
  const handleIamRoleChange = ({ detail }) => {
    setIamRole(detail.value);
    setErrors((prev) => ({
      ...prev,
      iamRole: "",
    }));
  };
  const handleCancel = () => {
    console.log("Cancel clicked - no changes saved");
  };
  const handleResetToDefaults = () => {
    console.log("Resetting to default values");
    setAwsRegion({
      description: "us-east-1",
      label: "US East (N. Virginia)",
      value: "us-east-1",
    });
    setS3Bucket("");
    setDynamoDbTable("");
    setIamRole("");
    setQueryLogPeriod(7);
    setSampleSize(1000);
    setTargetDatabases({
      dynamodb: true,
      documentdb: true,
      elasticache: true,
      opensearch: true,
      neptune: false,
      keyspaces: false,
      aurora: true,
    });
    setPiiAnonymization(true);
    setSampleDataCollection(true);
    setColorTheme("system");
    setAutoRefreshInterval({
      label: "Every 30 seconds",
      value: "30",
    });
    setBrowserNotifications(false);
    setEmailNotifications(false);
    setNotificationEvents({
      completed: false,
      failed: false,
      warnings: false,
      longRunning: false,
    });
    setCompactMode(false);
    setErrors({
      s3Bucket: "",
      dynamoDbTable: "",
      iamRole: "",
    });
  };
  const handleTestConnection = () => {
    console.log("Testing AWS connection with:", {
      region: awsRegion.value,
      s3Bucket,
      dynamoDbTable,
      iamRole,
    });
    console.log(
      "Testing AWS connection... (This would connect to AWS services in a real application)",
    );
  };
  const handleSaveSettings = () => {
    console.log("Validating and saving settings...");

    // Validate all fields
    const s3Error = validateS3Bucket(s3Bucket);
    const dynamoError = validateDynamoDbTable(dynamoDbTable);
    const iamError = validateIamRole(iamRole);
    const newErrors = {
      s3Bucket: s3Error,
      dynamoDbTable: dynamoError,
      iamRole: iamError,
    };
    setErrors(newErrors);

    // Check if there are any errors
    if (s3Error || dynamoError || iamError) {
      console.log("Validation errors:", newErrors);
      return;
    }

    // Collect all settings
    const settings = {
      awsConfiguration: {
        region: awsRegion.value,
        s3Bucket,
        dynamoDbTable,
        iamRole,
      },
      defaultAnalysisOptions: {
        queryLogPeriod,
        sampleSize,
        targetDatabases,
        piiAnonymization,
        sampleDataCollection,
      },
      uiPreferences: {
        colorTheme,
        autoRefreshInterval: autoRefreshInterval.value,
        browserNotifications,
        emailNotifications,
        notificationEvents,
        compactMode,
      },
    };
    console.log("Settings saved successfully:", settings);
    // TODO: Replace with proper notification/flashbar
    console.log("Settings saved successfully!");
  };
  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
      breadcrumbs={
        <BreadcrumbGroup
          items={[
            {
              href: "#db-mod-dash",
              text: t("settings.breadcrumb.home"),
            },
            {
              href: "#db-mod-settings",
              text: t("settings.breadcrumb.settings"),
            },
          ]}
        />
      }
      content={
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleCancel}>
                {t("common.actions.cancel")}
              </Button>
              <Button variant="normal" onClick={handleResetToDefaults}>
                {t("settings.actions.reset-to-defaults")}
              </Button>
              <Button
                iconName="status-positive"
                variant="normal"
                onClick={handleTestConnection}
              >
                {t("settings.actions.test-aws-connection")}
              </Button>
              <Button variant="primary" onClick={handleSaveSettings}>
                {t("settings.actions.save-settings")}
              </Button>
            </SpaceBetween>
          }
          header={
            <Header
              description={t("settings.header.description")}
              variant="h1"
            >
              {t("settings.header.title")}
            </Header>
          }
        >
          <SpaceBetween size="l">
            {/* AWS Configuration Container */}
            <Container
              header={
                <Header
                  description={t("settings.aws-config.header.description")}
                  variant="h2"
                >
                  {t("settings.aws-config.header.title")}
                </Header>
              }
             
            >
              <SpaceBetween size="l">
                <FormField
                  constraintText={t("settings.aws-config.region.constraint-text")}
                  description={t("settings.aws-config.region.description")}
                  label={t("settings.aws-config.region.label")}
                >
                  <Select
                    filteringPlaceholder={t("settings.aws-config.region.filtering-placeholder")}
                    filteringType="auto"
                    options={regionOptions}
                    placeholder={t("settings.aws-config.region.placeholder")}
                    selectedOption={awsRegion}
                    onChange={({ detail }) =>
                      setAwsRegion(detail.selectedOption)
                    }
                  />
                </FormField>

                <FormField
                  constraintText={t("settings.aws-config.s3-bucket.constraint-text")}
                  description={t("settings.aws-config.s3-bucket.description")}
                  label={t("settings.aws-config.s3-bucket.label")}
                  errorText={errors.s3Bucket}
                  secondaryControl={
                    <Button
                      iconAlign="right"
                      iconName="external"
                      variant="normal"
                      onClick={() =>
                        console.log("Opening S3 bucket creation page")
                      }
                    >
                      {t("settings.aws-config.s3-bucket.create-button")}
                    </Button>
                  }
                >
                  <Input
                    placeholder={t("settings.aws-config.s3-bucket.placeholder")}
                    type="text"
                    value={s3Bucket}
                    onChange={handleS3BucketChange}
                  />
                </FormField>

                <FormField
                  constraintText={t("settings.aws-config.dynamodb-table.constraint-text")}
                  description={t("settings.aws-config.dynamodb-table.description")}
                  label={t("settings.aws-config.dynamodb-table.label")}
                  errorText={errors.dynamoDbTable}
                >
                  <Input
                    placeholder={t("settings.aws-config.dynamodb-table.placeholder")}
                    type="text"
                    value={dynamoDbTable}
                    onChange={handleDynamoDbTableChange}
                  />
                </FormField>

                <FormField
                  constraintText={t("settings.aws-config.iam-role.constraint-text")}
                  description={t("settings.aws-config.iam-role.description")}
                  label={t("settings.aws-config.iam-role.label")}
                  errorText={errors.iamRole}
                >
                  <Input
                    placeholder={t("settings.aws-config.iam-role.placeholder")}
                    type="text"
                    value={iamRole}
                    onChange={handleIamRoleChange}
                  />
                </FormField>
              </SpaceBetween>
            </Container>

            {/* Default Analysis Options Container */}
            <Container
              header={
                <Header
                  description={t("settings.analysis-options.header.description")}
                  variant="h2"
                >
                  {t("settings.analysis-options.header.title")}
                </Header>
              }
             
            >
              <SpaceBetween size="l">
                <FormField
                  constraintText={t("settings.analysis-options.query-log-period.constraint-text")}
                  description={t("settings.analysis-options.query-log-period.description")}
                  label={t("settings.analysis-options.query-log-period.label")}
                  stretch
                >
                  <Slider
                    max={30}
                    min={1}
                    referenceValues={[7, 14, 21]}
                    step={1}
                    value={queryLogPeriod}
                    onChange={({ detail }) => setQueryLogPeriod(detail.value)}
                  />
                </FormField>

                <FormField
                  constraintText={t("settings.analysis-options.sample-size.constraint-text")}
                  description={t("settings.analysis-options.sample-size.description")}
                  label={t("settings.analysis-options.sample-size.label")}
                  stretch
                >
                  <Slider
                    max={10000}
                    min={100}
                    referenceValues={[2500, 5000, 7500]}
                    step={100}
                    tickMarks
                    value={sampleSize}
                    onChange={({ detail }) => setSampleSize(detail.value)}
                  />
                </FormField>

                <FormField
                  description={t("settings.analysis-options.target-databases.description")}
                  label={t("settings.analysis-options.target-databases.label")}
                  stretch
                >
                  <SpaceBetween size="m">
                    <Checkbox
                      checked={targetDatabases.dynamodb}
                      onChange={({ detail }) =>
                        setTargetDatabases((prev) => ({
                          ...prev,
                          dynamodb: detail.checked,
                        }))
                      }
                      description={t("settings.analysis-options.target-databases.dynamodb-description")}
                    >
                      {t("common.databases.dynamodb")}
                    </Checkbox>
                    <Checkbox
                      checked={targetDatabases.documentdb}
                      onChange={({ detail }) =>
                        setTargetDatabases((prev) => ({
                          ...prev,
                          documentdb: detail.checked,
                        }))
                      }
                      description={t("settings.analysis-options.target-databases.documentdb-description")}
                    >
                      {t("common.databases.documentdb")}
                    </Checkbox>
                    <Checkbox
                      checked={targetDatabases.elasticache}
                      onChange={({ detail }) =>
                        setTargetDatabases((prev) => ({
                          ...prev,
                          elasticache: detail.checked,
                        }))
                      }
                      description={t("settings.analysis-options.target-databases.elasticache-description")}
                    >
                      {t("common.databases.elasticache")}
                    </Checkbox>
                    <Checkbox
                      checked={targetDatabases.opensearch}
                      onChange={({ detail }) =>
                        setTargetDatabases((prev) => ({
                          ...prev,
                          opensearch: detail.checked,
                        }))
                      }
                      description={t("settings.analysis-options.target-databases.opensearch-description")}
                    >
                      {t("common.databases.opensearch")}
                    </Checkbox>
                    <Checkbox
                      checked={targetDatabases.neptune}
                      onChange={({ detail }) =>
                        setTargetDatabases((prev) => ({
                          ...prev,
                          neptune: detail.checked,
                        }))
                      }
                      description={t("settings.analysis-options.target-databases.neptune-description")}
                    >
                      {t("common.databases.neptune")}
                    </Checkbox>
                    <Checkbox
                      checked={targetDatabases.keyspaces}
                      onChange={({ detail }) =>
                        setTargetDatabases((prev) => ({
                          ...prev,
                          keyspaces: detail.checked,
                        }))
                      }
                      description={t("settings.analysis-options.target-databases.keyspaces-description")}
                    >
                      {t("common.databases.keyspaces")}
                    </Checkbox>
                    <Checkbox
                      checked={targetDatabases.aurora}
                      onChange={({ detail }) =>
                        setTargetDatabases((prev) => ({
                          ...prev,
                          aurora: detail.checked,
                        }))
                      }
                      description={t("settings.analysis-options.target-databases.aurora-description")}
                    >
                      {t("common.databases.aurora")}
                    </Checkbox>
                  </SpaceBetween>
                </FormField>

                <FormField
                  description={t("settings.analysis-options.pii-anonymization.description")}
                  label={t("settings.analysis-options.pii-anonymization.label")}
                >
                  <Checkbox
                    checked={piiAnonymization}
                    onChange={({ detail }) =>
                      setPiiAnonymization(detail.checked)
                    }
                    description={t("settings.analysis-options.pii-anonymization.checkbox-description")}
                  >
                    {t("settings.analysis-options.pii-anonymization.checkbox-label")}
                  </Checkbox>
                </FormField>

                <FormField
                  description={t("settings.analysis-options.sample-data.description")}
                  label={t("settings.analysis-options.sample-data.label")}
                >
                  <Checkbox
                    checked={sampleDataCollection}
                    onChange={({ detail }) =>
                      setSampleDataCollection(detail.checked)
                    }
                    description={t("settings.analysis-options.sample-data.checkbox-description")}
                  >
                    {t("settings.analysis-options.sample-data.checkbox-label")}
                  </Checkbox>
                </FormField>
              </SpaceBetween>
            </Container>

            {/* UI Preferences Container */}
            <Container
              header={
                <Header
                  description={t("settings.ui-preferences.header.description")}
                  variant="h2"
                >
                  {t("settings.ui-preferences.header.title")}
                </Header>
              }
             
            >
              <SpaceBetween size="l">
                <FormField
                  description={t("settings.ui-preferences.color-theme.description")}
                  label={t("settings.ui-preferences.color-theme.label")}
                  stretch
                >
                  <RadioGroup
                    items={themeOptions}
                    value={colorTheme}
                    onChange={({ detail }) => setColorTheme(detail.value)}
                  />
                </FormField>

                <FormField
                  constraintText={t("settings.ui-preferences.auto-refresh.constraint-text")}
                  description={t("settings.ui-preferences.auto-refresh.description")}
                  label={t("settings.ui-preferences.auto-refresh.label")}
                >
                  <Select
                    options={refreshIntervalOptions}
                    placeholder={t("settings.ui-preferences.auto-refresh.placeholder")}
                    selectedOption={autoRefreshInterval}
                    onChange={({ detail }) =>
                      setAutoRefreshInterval(detail.selectedOption)
                    }
                  />
                </FormField>

                <FormField
                  description={t("settings.ui-preferences.browser-notifications.description")}
                  label={t("settings.ui-preferences.browser-notifications.label")}
                >
                  <Toggle
                    checked={browserNotifications}
                    onChange={({ detail }) =>
                      setBrowserNotifications(detail.checked)
                    }
                    description={t("settings.ui-preferences.browser-notifications.toggle-description")}
                  >
                    {t("settings.ui-preferences.browser-notifications.toggle-label")}
                  </Toggle>
                </FormField>

                <FormField
                  description={t("settings.ui-preferences.email-notifications.description")}
                  label={t("settings.ui-preferences.email-notifications.label")}
                >
                  <Toggle
                    checked={emailNotifications}
                    onChange={({ detail }) =>
                      setEmailNotifications(detail.checked)
                    }
                    description={t("settings.ui-preferences.email-notifications.toggle-description")}
                  >
                    {t("settings.ui-preferences.email-notifications.toggle-label")}
                  </Toggle>
                </FormField>

                <FormField
                  description={t("settings.ui-preferences.notification-events.description")}
                  label={t("settings.ui-preferences.notification-events.label")}
                  stretch
                >
                  <SpaceBetween size="m">
                    <Toggle
                      checked={notificationEvents.completed}
                      onChange={({ detail }) =>
                        setNotificationEvents((prev) => ({
                          ...prev,
                          completed: detail.checked,
                        }))
                      }
                      description={t("settings.ui-preferences.notification-events.completed-description")}
                    >
                      {t("settings.ui-preferences.notification-events.completed-label")}
                    </Toggle>
                    <Toggle
                      checked={notificationEvents.failed}
                      onChange={({ detail }) =>
                        setNotificationEvents((prev) => ({
                          ...prev,
                          failed: detail.checked,
                        }))
                      }
                      description={t("settings.ui-preferences.notification-events.failed-description")}
                    >
                      {t("settings.ui-preferences.notification-events.failed-label")}
                    </Toggle>
                    <Toggle
                      checked={notificationEvents.warnings}
                      onChange={({ detail }) =>
                        setNotificationEvents((prev) => ({
                          ...prev,
                          warnings: detail.checked,
                        }))
                      }
                      description={t("settings.ui-preferences.notification-events.warnings-description")}
                    >
                      {t("settings.ui-preferences.notification-events.warnings-label")}
                    </Toggle>
                    <Toggle
                      checked={notificationEvents.longRunning}
                      onChange={({ detail }) =>
                        setNotificationEvents((prev) => ({
                          ...prev,
                          longRunning: detail.checked,
                        }))
                      }
                      description={t("settings.ui-preferences.notification-events.long-running-description")}
                    >
                      {t("settings.ui-preferences.notification-events.long-running-label")}
                    </Toggle>
                  </SpaceBetween>
                </FormField>

                <FormField
                  description={t("settings.ui-preferences.compact-mode.description")}
                  label={t("settings.ui-preferences.compact-mode.label")}
                >
                  <Toggle
                    checked={compactMode}
                    onChange={({ detail }) => setCompactMode(detail.checked)}
                    description={t("settings.ui-preferences.compact-mode.toggle-description")}
                  >
                    {t("settings.ui-preferences.compact-mode.toggle-label")}
                  </Toggle>
                </FormField>
              </SpaceBetween>
            </Container>
          </SpaceBetween>
        </Form>
      }
      contentType="form"
      navigation={
        <SideNavigation
          activeHref="#db-mod-settings"
          header={SideNavigationConfigurations.header}
          items={SideNavigationConfigurations.items}
        />
      }
      toolsHide
    />
    </>
  );
}
export default SettingsPage;
