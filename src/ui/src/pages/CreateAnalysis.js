//##-- React Events
import { useState, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Wizard from "@cloudscape-design/components/wizard";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import FormField from "@cloudscape-design/components/form-field";
import Select from "@cloudscape-design/components/select";
import Input from "@cloudscape-design/components/input";
import RadioGroup from "@cloudscape-design/components/radio-group";
import Toggle from "@cloudscape-design/components/toggle";
import Slider from "@cloudscape-design/components/slider";
import Checkbox from "@cloudscape-design/components/checkbox";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Textarea from "@cloudscape-design/components/textarea";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import Tiles from "@cloudscape-design/components/tiles";
import Box from "@cloudscape-design/components/box";
import FileUpload from "@cloudscape-design/components/file-upload";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Alert from "@cloudscape-design/components/alert";
import ColumnLayout from "@cloudscape-design/components/column-layout";



//##-- Custom Objects
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
function SourceDatabaseSection({ formData, setFormData, errors }) {
  const { t } = useTranslation();
  const rdsOptions = [
    {
      description: "MySQL 8.0.32 | db.r5.large | us-east-1a",
      label: "mysql-prod-01",
      tags: ["Production", "MySQL"],
      value: "mysql-prod-01",
    },
    {
      description: "PostgreSQL 14.7 | db.t3.medium | us-east-1b",
      label: "postgres-dev-01",
      tags: ["Development", "PostgreSQL"],
      value: "postgres-dev-01",
    },
    {
      description: "MariaDB 10.6 | db.m5.xlarge | us-east-1c",
      label: "mariadb-staging-01",
      tags: ["Staging", "MariaDB"],
      value: "mariadb-staging-01",
    },
  ];
  const credentialOptions = [
    {
      description: "Provide the ARN of a secret stored in AWS Secrets Manager.",
      label: "AWS Secrets Manager ARN",
      value: "secrets-arn",
    },
    {
      description:
        "Provide the name of a secret stored in AWS Secrets Manager.",
      label: "AWS Secrets Manager name",
      value: "secrets-name",
    },
    {
      description:
        "Enter username and password directly. Not recommended for production use.",
      label: "Direct credentials",
      value: "direct",
    },
  ];
  return (
    <SpaceBetween size="l">
      <FormField
        description={t("create-analysis.source.rds-instance.description")}
        label={t("create-analysis.source.rds-instance.label")}
      >
        <Select
          filteringPlaceholder={t("create-analysis.source.rds-instance.filtering-placeholder")}
          filteringType="auto"
          options={rdsOptions}
          placeholder={t("create-analysis.source.rds-instance.placeholder")}
          selectedOption={formData.rdsInstance}
          onChange={({ detail }) =>
            setFormData({
              ...formData,
              rdsInstance: detail.selectedOption,
            })
          }
        />
      </FormField>

      <FormField
        constraintText={t("create-analysis.source.host.constraint-text")}
        description={t("create-analysis.source.host.description")}
        label={t("create-analysis.source.host.label")}
        errorText={errors.host}
      >
        <Input
          placeholder="database.example.com"
          type="text"
          value={formData.host}
          onChange={({ detail }) =>
            setFormData({
              ...formData,
              host: detail.value,
            })
          }
        />
      </FormField>

      <SpaceBetween direction="horizontal" size="l">
        <FormField
          constraintText={t("create-analysis.source.port.constraint-text")}
          description={t("create-analysis.source.port.description")}
          label={t("create-analysis.source.port.label")}
          errorText={errors.port}
        >
          <Input
            placeholder="3306"
            type="number"
            value={formData.port}
            onChange={({ detail }) =>
              setFormData({
                ...formData,
                port: detail.value,
              })
            }
          />
        </FormField>

        <FormField
          description={t("create-analysis.source.database-name.description")}
          label={t("create-analysis.source.database-name.label")}
          errorText={errors.databaseName}
        >
          <Input
            placeholder="mydatabase"
            type="text"
            value={formData.databaseName}
            onChange={({ detail }) =>
              setFormData({
                ...formData,
                databaseName: detail.value,
              })
            }
          />
        </FormField>
      </SpaceBetween>

      <FormField
        description={t("create-analysis.source.credentials.description")}
        label={t("create-analysis.source.credentials.label")}
        stretch
      >
        <RadioGroup
          items={credentialOptions}
          value={formData.credentialsType}
          onChange={({ detail }) =>
            setFormData({
              ...formData,
              credentialsType: detail.value,
            })
          }
        />
      </FormField>

      {formData.credentialsType === "secrets-arn" && (
        <FormField
          constraintText={t("create-analysis.source.secret-arn.constraint-text")}
          label={t("create-analysis.source.secret-arn.label")}
          errorText={errors.secretArn}
        >
          <Input
            placeholder="arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db/credentials"
            type="text"
            value={formData.secretArn}
            onChange={({ detail }) =>
              setFormData({
                ...formData,
                secretArn: detail.value,
              })
            }
          />
        </FormField>
      )}

      {formData.credentialsType === "secrets-name" && (
        <FormField
          constraintText={t("create-analysis.source.secret-name.constraint-text")}
          label={t("create-analysis.source.secret-name.label")}
          errorText={errors.secretName}
        >
          <Input
            placeholder="prod/db/credentials"
            type="text"
            value={formData.secretName}
            onChange={({ detail }) =>
              setFormData({
                ...formData,
                secretName: detail.value,
              })
            }
          />
        </FormField>
      )}

      {formData.credentialsType === "direct" && (
        <SpaceBetween size="l">
          <FormField label={t("create-analysis.source.username.label")} errorText={errors.username}>
            <Input
              placeholder="admin"
              type="text"
              value={formData.username}
              onChange={({ detail }) =>
                setFormData({
                  ...formData,
                  username: detail.value,
                })
              }
            />
          </FormField>
          <FormField label={t("create-analysis.source.password.label")} errorText={errors.password}>
            <Input
              placeholder={t("create-analysis.source.password.placeholder")}
              type="password"
              value={formData.password}
              onChange={({ detail }) =>
                setFormData({
                  ...formData,
                  password: detail.value,
                })
              }
            />
          </FormField>
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
function CollectionOptionsSection({ formData, setFormData }) {
  const { t } = useTranslation();
  const sampleSizeOptions = [
    {
      label: "100 rows",
      value: "100",
    },
    {
      label: "500 rows",
      value: "500",
    },
    {
      label: "1,000 rows",
      value: "1000",
    },
    {
      label: "2,500 rows",
      value: "2500",
    },
    {
      label: "5,000 rows",
      value: "5000",
    },
    {
      label: "7,500 rows",
      value: "7500",
    },
    {
      label: "10,000 rows",
      value: "10000",
    },
  ];
  const queryLogPeriodOptions = [
    {
      label: "1 day",
      value: "1",
    },
    {
      label: "3 days",
      value: "3",
    },
    {
      label: "7 days",
      value: "7",
    },
    {
      label: "14 days",
      value: "14",
    },
    {
      label: "21 days",
      value: "21",
    },
    {
      label: "30 days",
      value: "30",
    },
  ];
  const queryLogSourceOptions = [
    {
      description:
        "AWS Performance Insights - comprehensive query and performance metrics",
      label: "Performance Insights (recommended)",
      tags: ["Recommended", "All databases"],
      value: "performance-insights",
    },
    {
      description: "MySQL performance schema - native query statistics",
      label: "performance_schema",
      tags: ["MySQL", "MariaDB"],
      value: "performance-schema",
    },
    {
      description: "PostgreSQL statistics extension - query execution metrics",
      label: "pg_stat_statements",
      tags: ["PostgreSQL"],
      value: "pg-stat-statements",
    },
    {
      description: "SQL Server DMVs - query execution statistics",
      label: "Dynamic Management Views (DMV)",
      tags: ["SQL Server"],
      value: "dmv",
    },
  ];
  return (
    <SpaceBetween size="l">
      <FormField
        description={t("create-analysis.collection.anonymize-pii.description")}
        label={t("create-analysis.collection.anonymize-pii.label")}
      >
        <Toggle
          checked={formData.anonymizePii}
          onChange={({ detail }) =>
            setFormData({
              ...formData,
              anonymizePii: detail.checked,
            })
          }
        >
          {t("create-analysis.collection.anonymize-pii.toggle-label")}
        </Toggle>
      </FormField>

      <FormField
        description={t("create-analysis.collection.include-sample-data.description")}
        label={t("create-analysis.collection.include-sample-data.label")}
      >
        <Toggle
          checked={formData.includeSampleData}
          onChange={({ detail }) =>
            setFormData({
              ...formData,
              includeSampleData: detail.checked,
            })
          }
        >
          {t("create-analysis.collection.include-sample-data.toggle-label")}
        </Toggle>
      </FormField>

      {formData.includeSampleData && (
        <FormField
          constraintText={t("create-analysis.collection.sample-size.constraint-text")}
          description={t("create-analysis.collection.sample-size.description")}
          label={t("create-analysis.collection.sample-size.label")}
          stretch
        >
          <SpaceBetween direction="horizontal" size="l">
            <Slider
              max={10000}
              min={100}
              referenceValues={[2500, 5000, 7500]}
              step={100}
              tickMarks
              value={formData.sampleSize}
              onChange={({ detail }) =>
                setFormData({
                  ...formData,
                  sampleSize: detail.value,
                })
              }
            />
            <FormField stretch>
              <Select
                options={sampleSizeOptions}
                selectedOption={
                  sampleSizeOptions.find(
                    (opt) => opt.value === String(formData.sampleSize),
                  ) || sampleSizeOptions[2]
                }
                onChange={({ detail }) =>
                  setFormData({
                    ...formData,
                    sampleSize: parseInt(detail.selectedOption.value),
                  })
                }
              />
            </FormField>
          </SpaceBetween>
        </FormField>
      )}

      <FormField
        constraintText={t("create-analysis.collection.query-log-period.constraint-text")}
        description={t("create-analysis.collection.query-log-period.description")}
        label={t("create-analysis.collection.query-log-period.label")}
        stretch
      >
        <SpaceBetween direction="horizontal" size="l">
          <Slider
            max={30}
            min={1}
            referenceValues={[7, 14, 21]}
            step={1}
            value={formData.queryLogPeriod}
            onChange={({ detail }) =>
              setFormData({
                ...formData,
                queryLogPeriod: detail.value,
              })
            }
          />
          <FormField stretch>
            <Select
              options={queryLogPeriodOptions}
              selectedOption={
                queryLogPeriodOptions.find(
                  (opt) => opt.value === String(formData.queryLogPeriod),
                ) || queryLogPeriodOptions[2]
              }
              onChange={({ detail }) =>
                setFormData({
                  ...formData,
                  queryLogPeriod: parseInt(detail.selectedOption.value),
                })
              }
            />
          </FormField>
        </SpaceBetween>
      </FormField>

      <FormField
        description={t("create-analysis.collection.query-log-source.description")}
        label={t("create-analysis.collection.query-log-source.label")}
      >
        <Select
          filteringType="auto"
          options={queryLogSourceOptions}
          placeholder={t("create-analysis.collection.query-log-source.placeholder")}
          selectedOption={formData.queryLogSource}
          onChange={({ detail }) =>
            setFormData({
              ...formData,
              queryLogSource: detail.selectedOption,
            })
          }
        />
      </FormField>
    </SpaceBetween>
  );
}
function TargetDatabasesSection({ formData, setFormData, errors }) {
  const { t } = useTranslation();
  const handleCheckboxChange = (key, checked) => {
    setFormData({
      ...formData,
      targetDatabases: {
        ...formData.targetDatabases,
        [key]: checked,
      },
    });
  };
  const handleSelectAll = () => {
    setFormData({
      ...formData,
      targetDatabases: {
        dynamodb: true,
        documentdb: true,
        elasticache: true,
        opensearch: true,
        neptune: true,
        keyspaces: true,
        aurora: true,
      },
    });
  };
  const handleDeselectAll = () => {
    setFormData({
      ...formData,
      targetDatabases: {
        dynamodb: false,
        documentdb: false,
        elasticache: false,
        opensearch: false,
        neptune: false,
        keyspaces: false,
        aurora: false,
      },
    });
  };
  return (
    <SpaceBetween size="l">
      <FormField
        description={t("create-analysis.target-databases.description")}
        label={t("create-analysis.target-databases.label")}
        stretch
        errorText={errors.targetDatabases}
      >
        <ColumnLayout columns={2}>
          <SpaceBetween size="m">
            <Checkbox
              checked={formData.targetDatabases.dynamodb}
              onChange={({ detail }) =>
                handleCheckboxChange("dynamodb", detail.checked)
              }
              description={t("create-analysis.target-databases.dynamodb-description")}
            >
              {t("common.databases.dynamodb")}
            </Checkbox>
            <Checkbox
              checked={formData.targetDatabases.documentdb}
              onChange={({ detail }) =>
                handleCheckboxChange("documentdb", detail.checked)
              }
              description={t("create-analysis.target-databases.documentdb-description")}
            >
              {t("common.databases.documentdb")}
            </Checkbox>
            <Checkbox
              checked={formData.targetDatabases.elasticache}
              onChange={({ detail }) =>
                handleCheckboxChange("elasticache", detail.checked)
              }
              description={t("create-analysis.target-databases.elasticache-description")}
            >
              {t("common.databases.elasticache")}
            </Checkbox>
            <Checkbox
              checked={formData.targetDatabases.opensearch}
              onChange={({ detail }) =>
                handleCheckboxChange("opensearch", detail.checked)
              }
              description={t("create-analysis.target-databases.opensearch-description")}
            >
              {t("common.databases.opensearch")}
            </Checkbox>
          </SpaceBetween>
          <SpaceBetween size="m">
            <Checkbox
              checked={false}
              disabled
              description={t("create-analysis.target-databases.neptune-description")}
            >
              {t("common.databases.neptune")}
            </Checkbox>
            <Checkbox
              checked={false}
              disabled
              description={t("create-analysis.target-databases.keyspaces-description")}
            >
              {t("common.databases.keyspaces")}
            </Checkbox>
            <Checkbox
              checked={false}
              disabled
              description={t("create-analysis.target-databases.aurora-description")}
            >
              {t("common.databases.aurora")}
            </Checkbox>
          </SpaceBetween>
        </ColumnLayout>
      </FormField>

      <SpaceBetween direction="horizontal" size="xs">
        <Button variant="normal" onClick={handleSelectAll}>
          {t("create-analysis.target-databases.select-all")}
        </Button>
        <Button variant="normal" onClick={handleDeselectAll}>
          {t("create-analysis.target-databases.deselect-all")}
        </Button>
      </SpaceBetween>
    </SpaceBetween>
  );
}
function AdvancedOptionsSection({ formData, setFormData, errors }) {
  const { t } = useTranslation();
  return (
    <SpaceBetween size="l">
      <FormField
        constraintText={t("create-analysis.advanced.include-patterns.constraint-text")}
        description={t("create-analysis.advanced.include-patterns.description")}
        label={t("create-analysis.advanced.include-patterns.label")}
        stretch
      >
        <Textarea
          placeholder="orders%, customer%, product_%"
          rows={3}
          value={formData.includeTablePatterns}
          onChange={({ detail }) =>
            setFormData({
              ...formData,
              includeTablePatterns: detail.value,
            })
          }
        />
      </FormField>

      <FormField
        constraintText={t("create-analysis.advanced.exclude-patterns.constraint-text")}
        description={t("create-analysis.advanced.exclude-patterns.description")}
        label={t("create-analysis.advanced.exclude-patterns.label")}
        stretch
      >
        <Textarea
          placeholder="temp_%, test_%, staging_%"
          rows={3}
          value={formData.excludeTablePatterns}
          onChange={({ detail }) =>
            setFormData({
              ...formData,
              excludeTablePatterns: detail.value,
            })
          }
        />
      </FormField>

      <FormField
        description={t("create-analysis.advanced.performance-insights.description")}
        label={t("create-analysis.advanced.performance-insights.label")}
        stretch
      >
        <SpaceBetween size="s">
          <FormField label={t("create-analysis.advanced.performance-insights.top-sql-label")}>
            <Input
              placeholder="100"
              type="number"
              value={formData.topSqlQueries}
              onChange={({ detail }) =>
                setFormData({
                  ...formData,
                  topSqlQueries: detail.value,
                })
              }
            />
          </FormField>
          <FormField label={t("create-analysis.advanced.performance-insights.top-wait-label")}>
            <Input
              placeholder="50"
              type="number"
              value={formData.topWaitEvents}
              onChange={({ detail }) =>
                setFormData({
                  ...formData,
                  topWaitEvents: detail.value,
                })
              }
            />
          </FormField>
        </SpaceBetween>
      </FormField>

      <FormField
        description={t("create-analysis.advanced.cloudwatch.description")}
        label={t("create-analysis.advanced.cloudwatch.label")}
        stretch
      >
        <SpaceBetween size="m">
          <FormField>
            <Input
              disabled
              placeholder="CPU utilization"
              readOnly
              value="enabled"
            />
          </FormField>
          <FormField>
            <Input
              disabled
              placeholder="Memory utilization"
              readOnly
              value="enabled"
            />
          </FormField>
          <FormField>
            <Input
              disabled
              placeholder="IOPS metrics"
              readOnly
              value="enabled"
            />
          </FormField>
          <FormField>
            <Input
              disabled
              placeholder="Network throughput"
              readOnly
              value="enabled"
            />
          </FormField>
        </SpaceBetween>
      </FormField>

      <ExpandableSection
        headerText={t("create-analysis.advanced.additional-settings.header")}
        variant="default"
      >
        <SpaceBetween size="l">
          <FormField
            constraintText={t("create-analysis.advanced.custom-query-patterns.constraint-text")}
            description={t("create-analysis.advanced.custom-query-patterns.description")}
            label={t("create-analysis.advanced.custom-query-patterns.label")}
            stretch
          >
            <Textarea
              placeholder="SELECT % FROM users WHERE %&#10;INSERT INTO % VALUES %"
              rows={4}
              value={formData.customQueryPatterns}
              onChange={({ detail }) =>
                setFormData({
                  ...formData,
                  customQueryPatterns: detail.value,
                })
              }
            />
          </FormField>

          <FormField
            constraintText={t("create-analysis.advanced.collection-interval.constraint-text")}
            description={t("create-analysis.advanced.collection-interval.description")}
            label={t("create-analysis.advanced.collection-interval.label")}
            errorText={errors.collectionInterval}
          >
            <Input
              placeholder="60"
              type="number"
              value={formData.collectionInterval}
              onChange={({ detail }) =>
                setFormData({
                  ...formData,
                  collectionInterval: detail.value,
                })
              }
            />
          </FormField>
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  );
}
function DatabaseAnalysisForm() {

  //##-- i18n
  const { t } = useTranslation();

  //##-- Navigation hook
  const navigate = useNavigate();


  //--|#######################| State Management Section  |#######################

  const [analysisMode, setAnalysisMode] = useState("online");
  const [activeStepIndex, setActiveStepIndex] = useState(0);

  const [formData, setFormData] = useState({
    // Source database
    rdsInstance: null,
    host: "",
    port: "",
    databaseName: "",
    credentialsType: "secrets-arn",
    secretArn: "",
    secretName: "",
    username: "",
    password: "",
    // Collection options
    anonymizePii: true,
    includeSampleData: true,
    sampleSize: 1000,
    queryLogPeriod: 7,
    queryLogSource: {
      label: "Performance Insights (recommended)",
      tags: ["Recommended"],
      value: "performance-insights",
    },
    // Target databases
    targetDatabases: {
      dynamodb: true,
      documentdb: true,
      elasticache: true,
      opensearch: true,
      neptune: false,
      keyspaces: false,
      aurora: true,
    },
    // Advanced options
    includeTablePatterns: "",
    excludeTablePatterns: "",
    topSqlQueries: "100",
    topWaitEvents: "50",
    customQueryPatterns: "",
    collectionInterval: "",
  });

  const [errors, setErrors] = useState({});
  const [loading, setLoading] = useState(false);
  const [flashbarItems, setFlashbarItems] = useState([]);

  // Offline upload state
  const [offlineFiles, setOfflineFiles] = useState([]);
  const [offlineDatabaseName, setOfflineDatabaseName] = useState("");
  const [offlineSourceType, setOfflineSourceType] = useState({ label: "MySQL", value: "mysql" });
  const [offlineTargetDatabases, setOfflineTargetDatabases] = useState({
    dynamodb: true, documentdb: true, elasticache: false,
    opensearch: true, neptune: false, keyspaces: false, aurora: false,
  });
  const [uploadState, setUploadState] = useState("idle"); // idle, preparing, uploading, confirming, ready, submitting
  const [uploadProgress, setUploadProgress] = useState(0);
  const [preparedJob, setPreparedJob] = useState(null); // { job_id, upload_url, upload_key }




  //--|#######################| Handle Section  |#######################


  //##-- Flashbar message handler
  const addFlashbarMessage = useCallback((message) => {
    setFlashbarItems(prevItems => [...prevItems, message]);
  }, []);

  const handleFlashbarDismiss = useCallback((itemId) => {
    setFlashbarItems(prevItems => prevItems.filter(item => item.id !== itemId));
  }, []);




  //--|#######################| Validation Section  |#######################

  const validateStep = (stepIndex) => {
    const newErrors = {};

    if (stepIndex === 0) {
      // Step 1: Source database validation
      if (!formData.host.trim()) {
        newErrors.host = "Host is required";
      }

      if (!formData.port.trim()) {
        newErrors.port = "Port is required";
      } else if (
        isNaN(formData.port) ||
        parseInt(formData.port) < 1 ||
        parseInt(formData.port) > 65535
      ) {
        newErrors.port = "Port must be a valid number between 1 and 65535";
      }

      if (!formData.databaseName.trim()) {
        newErrors.databaseName = "Database name is required";
      }

      if (formData.credentialsType === "secrets-arn") {
        if (!formData.secretArn.trim()) {
          newErrors.secretArn = "Secret ARN is required";
        } else if (!formData.secretArn.startsWith("arn:aws:secretsmanager:")) {
          newErrors.secretArn = "Please enter a valid AWS Secrets Manager ARN";
        }
      } else if (formData.credentialsType === "secrets-name") {
        if (!formData.secretName.trim()) {
          newErrors.secretName = "Secret name is required";
        }
      } else if (formData.credentialsType === "direct") {
        if (!formData.username.trim()) {
          newErrors.username = "Username is required";
        }
        if (!formData.password.trim()) {
          newErrors.password = "Password is required";
        }
      }
    } else if (stepIndex === 2) {
      // Step 3: Target databases validation
      const hasSelectedDatabase = Object.values(formData.targetDatabases).some(
        (val) => val === true
      );
      if (!hasSelectedDatabase) {
        newErrors.targetDatabases = "Please select at least one target database";
      }
    } else if (stepIndex === 3) {
      // Step 4: Advanced options validation
      if (formData.collectionInterval.trim()) {
        const interval = parseInt(formData.collectionInterval);
        if (isNaN(interval) || interval < 1 || interval > 1440) {
          newErrors.collectionInterval =
            "Collection interval must be between 1 and 1440 minutes";
        }
      }
    }

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const validateForm = () => {
    const newErrors = {};

    // Validate host (required)
    if (!formData.host.trim()) {
      newErrors.host = "Host is required";
    }

    // Validate port (required and must be a valid number)
    if (!formData.port.trim()) {
      newErrors.port = "Port is required";
    } else if (
      isNaN(formData.port) ||
      parseInt(formData.port) < 1 ||
      parseInt(formData.port) > 65535
    ) {
      newErrors.port = "Port must be a valid number between 1 and 65535";
    }

    // Validate database name (required)
    if (!formData.databaseName.trim()) {
      newErrors.databaseName = "Database name is required";
    }

    // Validate credentials based on type
    if (formData.credentialsType === "secrets-arn") {
      if (!formData.secretArn.trim()) {
        newErrors.secretArn = "Secret ARN is required";
      } else if (!formData.secretArn.startsWith("arn:aws:secretsmanager:")) {
        newErrors.secretArn = "Please enter a valid AWS Secrets Manager ARN";
      }
    } else if (formData.credentialsType === "secrets-name") {
      if (!formData.secretName.trim()) {
        newErrors.secretName = "Secret name is required";
      }
    } else if (formData.credentialsType === "direct") {
      if (!formData.username.trim()) {
        newErrors.username = "Username is required";
      }
      if (!formData.password.trim()) {
        newErrors.password = "Password is required";
      }
    }

    // Validate at least one target database is selected
    const hasSelectedDatabase = Object.values(formData.targetDatabases).some(
      (val) => val === true,
    );
    if (!hasSelectedDatabase) {
      newErrors.targetDatabases = "Please select at least one target database";
    }

    // Validate collection interval if provided
    if (formData.collectionInterval.trim()) {
      const interval = parseInt(formData.collectionInterval);
      if (isNaN(interval) || interval < 1 || interval > 1440) {
        newErrors.collectionInterval =
          "Collection interval must be between 1 and 1440 minutes";
      }
    }
    setErrors(newErrors);

    // Log errors for debugging
    if (Object.keys(newErrors).length > 0) {
      console.log("Validation errors:", newErrors);
    }
    return Object.keys(newErrors).length === 0;
  };




  //--|#######################| API Call Section  |#######################


  //##-- Upload file for offline mode (prepare → upload → confirm)
  const handleOfflineUpload = useCallback(async () => {
    if (!offlineFiles.length) {
      addFlashbarMessage({ type: 'error', header: 'No file selected', content: 'Please select a collector output JSON file to upload.', dismissible: true, id: `error-${Date.now()}` });
      return;
    }
    if (!offlineDatabaseName.trim()) {
      addFlashbarMessage({ type: 'error', header: 'Database name required', content: 'Please enter a database name for this assessment.', dismissible: true, id: `error-${Date.now()}` });
      return;
    }

    setLoading(true);
    setFlashbarItems([]);

    try {
      const apiManager = new ApiManager();

      // Step 1: Prepare — get job_id + presigned URL
      setUploadState("preparing");
      const prepareResults = await apiManager.execute([{
        id: 'prepare',
        path: 'assessments/prepare',
        method: 'POST',
        params: {
          database_name: offlineDatabaseName.trim(),
          source_database_type: offlineSourceType.value,
        }
      }]);

      if (prepareResults['prepare']?.error) {
        throw new Error(prepareResults['prepare'].error?.message || 'Failed to prepare upload');
      }

      const prepared = prepareResults['prepare'];
      const { job_id, upload_url, upload_key } = prepared;
      setPreparedJob({ job_id, upload_url, upload_key });

      // Step 2: Upload file directly to S3 via presigned URL
      setUploadState("uploading");
      const file = offlineFiles[0];

      await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', upload_url, true);
        xhr.setRequestHeader('Content-Type', 'application/json');

        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) {
            setUploadProgress(Math.round((event.loaded / event.total) * 100));
          }
        };

        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve();
          } else {
            reject(new Error(`Upload failed with status ${xhr.status}`));
          }
        };

        xhr.onerror = () => reject(new Error('Network error during upload'));
        xhr.ontimeout = () => reject(new Error('Upload timed out'));
        xhr.timeout = 300000; // 5 minutes
        xhr.send(file);
      });

      // Step 3: Confirm upload
      setUploadState("confirming");
      const confirmResults = await apiManager.execute([{
        id: 'confirm',
        path: `assessments/${job_id}/uploads/confirm?database_name=${encodeURIComponent(offlineDatabaseName.trim())}`,
        method: 'POST',
        params: {}
      }]);

      if (confirmResults['confirm']?.error) {
        throw new Error(confirmResults['confirm'].error?.message || 'Upload confirmation failed — file may not have reached S3');
      }

      const confirmed = confirmResults['confirm'];
      setUploadState("ready");

      addFlashbarMessage({
        type: 'success',
        header: 'File uploaded',
        content: `${file.name} (${(confirmed.size_bytes / 1024).toFixed(1)} KB) uploaded and confirmed. Ready to start analysis.`,
        dismissible: true,
        id: `success-upload-${Date.now()}`
      });

    } catch (error) {
      console.error('Offline upload error:', error);
      setUploadState("idle");
      setPreparedJob(null);
      addFlashbarMessage({
        type: 'error',
        header: 'Upload failed',
        content: error.message || 'An unexpected error occurred during upload.',
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setLoading(false);
    }
  }, [offlineFiles, offlineDatabaseName, offlineSourceType, addFlashbarMessage]);


  //##-- Start offline assessment after upload is confirmed
  const createOfflineAssessment = useCallback(async () => {
    if (!preparedJob) {
      addFlashbarMessage({ type: 'error', header: 'No upload', content: 'Please upload a file first.', dismissible: true, id: `error-${Date.now()}` });
      return;
    }

    setLoading(true);
    setUploadState("submitting");

    try {
      const apiManager = new ApiManager();
      const targetDatabasesArray = Object.keys(offlineTargetDatabases).filter(k => offlineTargetDatabases[k]);

      const results = await apiManager.execute([{
        id: 'create-assessment',
        path: 'assessments',
        method: 'POST',
        params: {
          job_id: preparedJob.job_id,
          source_database_type: offlineSourceType.value,
          database_name: offlineDatabaseName.trim(),
          collection_mode: "offline",
          offline_s3_key: preparedJob.upload_key,
          target_databases: targetDatabasesArray,
          full_analysis: false,
        }
      }]);

      if (results['create-assessment']?.error) {
        const result = results['create-assessment'];
        throw new Error(result.error?.message || 'Failed to create assessment');
      }

      const jobId = results['create-assessment'].job_id;
      addFlashbarMessage({
        type: 'success',
        header: 'Assessment started',
        content: `Offline assessment created with Job ID: ${jobId}. Redirecting to monitoring...`,
        dismissible: true,
        id: `success-${Date.now()}`
      });

      setTimeout(() => navigate(`/analysis/monitor/summary/${jobId}`), 2000);

    } catch (error) {
      console.error('Error creating offline assessment:', error);
      setUploadState("ready"); // Allow retry
      addFlashbarMessage({
        type: 'error',
        header: 'Assessment creation failed',
        content: error.message || 'Failed to start assessment.',
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setLoading(false);
    }
  }, [preparedJob, offlineDatabaseName, offlineSourceType, offlineTargetDatabases, addFlashbarMessage, navigate]);


  //##-- Create assessment for online mode
  const createAssessment = useCallback(async () => {
    if (!validateForm()) {
      addFlashbarMessage({
        type: 'error',
        header: 'Validation Error',
        content: 'Please fix the form errors before submitting',
        dismissible: true,
        id: `error-${Date.now()}`
      });
      return;
    }

    setLoading(true);
    setFlashbarItems([]);

    try {
      const apiManager = new ApiManager();

      // Build target databases array
      const targetDatabasesArray = Object.keys(formData.targetDatabases)
        .filter(key => formData.targetDatabases[key]);

      // Build connection object
      const connection = {
        host: formData.host,
        port: parseInt(formData.port),
        database: formData.databaseName,
        credentials_type: formData.credentialsType
      };

      // Add credentials based on type
      if (formData.credentialsType === 'secrets-arn') {
        connection.secret_arn = formData.secretArn;
      } else if (formData.credentialsType === 'secrets-name') {
        connection.secret_name = formData.secretName;
      } else if (formData.credentialsType === 'direct') {
        connection.username = formData.username;
        connection.password = formData.password;
      }

      // Build options object
      const options = {
        anonymize_pii: formData.anonymizePii,
        include_sample_data: formData.includeSampleData,
        sample_size: formData.sampleSize,
        query_log_period_days: formData.queryLogPeriod,
        query_log_source: formData.queryLogSource.value,
        top_sql_queries: parseInt(formData.topSqlQueries),
        top_wait_events: parseInt(formData.topWaitEvents)
      };

      // Add optional fields
      if (formData.includeTablePatterns.trim()) {
        options.include_table_patterns = formData.includeTablePatterns.trim();
      }
      if (formData.excludeTablePatterns.trim()) {
        options.exclude_table_patterns = formData.excludeTablePatterns.trim();
      }

      const apiCalls = [
        {
          id: 'create-assessment',
          path: 'assessments',
          method: 'POST',
          params: {
            source_database_type: "mysql", // TODO: Get from RDS instance or form
            database_name: formData.databaseName,
            connection: connection,
            options: options,
            target_databases: targetDatabasesArray,
            full_analysis: false
          }
        }
      ];

      const results = await apiManager.execute(apiCalls);
      console.log('Create assessment result:', results);

      if (results['create-assessment']?.error) {
        const result = results['create-assessment'];
        const errorMessage = result.error?.message || 'Failed to create assessment';
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments`;

        addFlashbarMessage({
          type: 'error',
          header: `API Error (Status: ${statusCode})`,
          content: `Failed to create assessment at '${apiUrl}': ${errorMessage}`,
          dismissible: true,
          id: `error-${Date.now()}`
        });
      } else if (results['create-assessment']?.success) {
        const jobId = results['create-assessment'].job_id;

        addFlashbarMessage({
          type: 'success',
          header: 'Assessment Created',
          content: `Assessment created successfully with Job ID: ${jobId}`,
          dismissible: true,
          id: `success-${Date.now()}`
        });

        // Navigate to job monitoring page after 2 seconds
        setTimeout(() => {
          navigate(`/analysis/monitor/summary/${jobId}`);
        }, 2000);
      }

    } catch (error) {
      console.error('Error creating assessment:', error);
      const errorDetails = error.message || 'Failed to create assessment';

      addFlashbarMessage({
        type: 'error',
        header: 'Unexpected Error',
        content: `An unexpected error occurred: ${errorDetails}. Please check your network connection and try again.`,
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setLoading(false);
    }
  }, [formData, validateForm, addFlashbarMessage, navigate]);




  //--|#######################| Wizard Steps Configuration  |#######################

  const wizardSteps = useMemo(() => [
    {
      title: t("create-analysis.step.source-database.title"),
      description: t("create-analysis.step.source-database.description"),
      content: (
        <SourceDatabaseSection
          formData={formData}
          setFormData={setFormData}
          errors={errors}
        />
      ),
      isOptional: false
    },
    {
      title: t("create-analysis.step.collection-options.title"),
      description: t("create-analysis.step.collection-options.description"),
      content: (
        <CollectionOptionsSection
          formData={formData}
          setFormData={setFormData}
        />
      ),
      isOptional: false
    },
    {
      title: t("create-analysis.step.target-databases.title"),
      description: t("create-analysis.step.target-databases.description"),
      content: (
        <TargetDatabasesSection
          formData={formData}
          setFormData={setFormData}
          errors={errors}
        />
      ),
      isOptional: false
    },
    {
      title: t("create-analysis.step.advanced-options.title"),
      description: t("create-analysis.step.advanced-options.description"),
      content: (
        <AdvancedOptionsSection
          formData={formData}
          setFormData={setFormData}
          errors={errors}
        />
      ),
      isOptional: true
    }
  ], [formData, errors, t]);


  //--|#######################| Action Handlers Section  |#######################


  const handleNavigate = useCallback((detail) => {
    const requestedStepIndex = detail.requestedStepIndex;

    // Validate current step before moving forward
    if (requestedStepIndex > activeStepIndex) {
      if (!validateStep(activeStepIndex)) {
        addFlashbarMessage({
          type: 'error',
          header: 'Validation Error',
          content: 'Please fix the errors in the current step before proceeding',
          dismissible: true,
          id: `error-${Date.now()}`
        });
        return;
      }
    }

    setActiveStepIndex(requestedStepIndex);
  }, [activeStepIndex, addFlashbarMessage, validateStep]);


  const handleCancel = useCallback(() => {
    console.log("Analysis cancelled");
    navigate('/dashboard');
  }, [navigate]);


  const handleSubmit = useCallback(() => {
    createAssessment();
  }, [createAssessment]);
  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        breadcrumbs={
          <BreadcrumbGroup
            items={[
              {
                href: "/",
                text: t("create-analysis.breadcrumb.home"),
              },
              {
                href: "/dashboard",
                text: t("create-analysis.breadcrumb.analyses"),
              },
              {
                href: "#",
                text: t("create-analysis.breadcrumb.create-analysis"),
              },
            ]}
          />
        }
        content={
          <SpaceBetween size="l">

            {flashbarItems.length > 0 && (
              <Flashbar
                items={flashbarItems.map(item => ({
                  ...item,
                  onDismiss: () => handleFlashbarDismiss(item.id)
                }))}
              />
            )}

            <SpaceBetween size="m">
              <Box variant="h1">{t("create-analysis.header.title")}</Box>
              <Box variant="p" color="text-body-secondary">
                {t("create-analysis.header.description")}
              </Box>

              <FormField
                label={t("create-analysis.form.analysis-mode.label")}
                description={t("create-analysis.form.analysis-mode.description")}
                stretch
              >
                <Tiles
                  items={[
                    {
                      value: "online",
                      label: t("create-analysis.form.analysis-mode.online-label"),
                      description: t("create-analysis.form.analysis-mode.online-description")
                    },
                    {
                      value: "offline",
                      label: t("create-analysis.form.analysis-mode.offline-label"),
                      description: t("create-analysis.form.analysis-mode.offline-description")
                    }
                  ]}
                  value={analysisMode}
                  onChange={({ detail }) => setAnalysisMode(detail.value)}
                />
              </FormField>
            </SpaceBetween>

            {analysisMode === "online" ? (
              <Wizard
                i18nStrings={{
                  stepNumberLabel: stepNumber => t("create-analysis.wizard.step-number-label", { stepNumber }),
                  collapsedStepsLabel: (stepNumber, stepsCount) =>
                    t("create-analysis.wizard.collapsed-steps-label", { stepNumber, stepsCount }),
                  skipToButtonLabel: (step, stepNumber) =>
                    t("create-analysis.wizard.skip-to-button-label", { title: step.title }),
                  navigationAriaLabel: t("create-analysis.wizard.navigation-aria-label"),
                  cancelButton: t("common.actions.cancel"),
                  previousButton: t("create-analysis.wizard.previous-button"),
                  nextButton: t("create-analysis.wizard.next-button"),
                  submitButton: t("create-analysis.wizard.submit-button"),
                  optional: t("create-analysis.wizard.optional")
                }}
                onNavigate={({ detail }) => handleNavigate(detail)}
                onCancel={handleCancel}
                onSubmit={handleSubmit}
                activeStepIndex={activeStepIndex}
                steps={wizardSteps}
                isLoadingNextStep={loading}
              />
            ) : (
              <SpaceBetween size="l">

                <Container header={<Header variant="h2">{t("create-analysis.offline.database-info.title")}</Header>}>
                  <ColumnLayout columns={2} variant="text-grid">
                    <FormField label={t("create-analysis.offline.database-info.database-name-label")} description={t("create-analysis.offline.database-info.database-name-description")} errorText={!offlineDatabaseName.trim() && errors.offlineDatabaseName}>
                      <Input
                        placeholder="my_database"
                        value={offlineDatabaseName}
                        onChange={({ detail }) => setOfflineDatabaseName(detail.value)}
                        disabled={uploadState !== "idle"}
                      />
                    </FormField>
                    <FormField label={t("create-analysis.offline.database-info.source-type-label")} description={t("create-analysis.offline.database-info.source-type-description")}>
                      <Select
                        selectedOption={offlineSourceType}
                        onChange={({ detail }) => setOfflineSourceType(detail.selectedOption)}
                        options={[
                          { label: "MySQL", value: "mysql" },
                          { label: "PostgreSQL", value: "postgresql" },
                          { label: "MariaDB", value: "mariadb" },
                          { label: "SQL Server", value: "sqlserver" },
                          { label: "Oracle", value: "oracle" },
                        ]}
                        disabled={uploadState !== "idle"}
                      />
                    </FormField>
                  </ColumnLayout>
                </Container>

                <Container header={<Header variant="h2">{t("create-analysis.offline.upload.title")}</Header>}>
                  <SpaceBetween size="l">
                    <Alert type="info">
                      {t("create-analysis.offline.upload.alert")}
                    </Alert>

                    <FormField label={t("create-analysis.offline.upload.file-label")} description={t("create-analysis.offline.upload.file-description")}>
                      <FileUpload
                        onChange={({ detail }) => {
                          setOfflineFiles(detail.value);
                          // Reset upload state if user changes file
                          if (uploadState !== "idle") {
                            setUploadState("idle");
                            setPreparedJob(null);
                            setUploadProgress(0);
                          }
                        }}
                        value={offlineFiles}
                        i18nStrings={{
                          uploadButtonText: e => e ? t("create-analysis.offline.upload.choose-files") : t("create-analysis.offline.upload.choose-file"),
                          dropzoneText: e => e ? t("create-analysis.offline.upload.drop-files") : t("create-analysis.offline.upload.drop-file"),
                          removeFileAriaLabel: e => t("create-analysis.offline.upload.remove-file", { index: e + 1 }),
                          limitShowFewer: t("create-analysis.offline.upload.show-fewer-files"),
                          limitShowMore: t("create-analysis.offline.upload.show-more-files"),
                          errorIconAriaLabel: t("create-analysis.offline.upload.error-icon-aria-label"),
                        }}
                        accept=".json"
                        constraintText={t("create-analysis.offline.upload.constraint-text")}
                        showFileSize
                        showFileLastModified
                        tokenLimit={1}
                      />
                    </FormField>

                    {(uploadState === "uploading" || uploadState === "preparing" || uploadState === "confirming") && (
                      <ProgressBar
                        value={uploadState === "preparing" ? 0 : uploadState === "confirming" ? 100 : uploadProgress}
                        label={t("create-analysis.offline.upload.progress-label")}
                        description={
                          uploadState === "preparing" ? t("create-analysis.offline.upload.progress-preparing") :
                          uploadState === "uploading" ? t("create-analysis.offline.upload.progress-uploading") :
                          t("create-analysis.offline.upload.progress-confirming")
                        }
                        status="in-progress"
                      />
                    )}

                    {uploadState === "ready" && (
                      <StatusIndicator type="success">
                        {t("create-analysis.offline.upload.status-ready")}
                      </StatusIndicator>
                    )}

                    {uploadState === "idle" && offlineFiles.length > 0 && (
                      <Button
                        variant="normal"
                        onClick={handleOfflineUpload}
                        loading={loading}
                        iconName="upload"
                      >
                        {t("create-analysis.offline.upload.upload-button")}
                      </Button>
                    )}
                  </SpaceBetween>
                </Container>

                <Container header={<Header variant="h2">{t("create-analysis.offline.target-databases.title")}</Header>}>
                  <FormField description={t("create-analysis.offline.target-databases.description")}>
                    <ColumnLayout columns={2}>
                      <SpaceBetween size="s">
                        {Object.entries({
                          dynamodb: "DynamoDB",
                          documentdb: "DocumentDB",
                          elasticache: "ElastiCache",
                          opensearch: "OpenSearch",
                        }).map(([key, label]) => (
                          <Checkbox
                            key={key}
                            checked={offlineTargetDatabases[key]}
                            onChange={({ detail }) => setOfflineTargetDatabases(prev => ({ ...prev, [key]: detail.checked }))}
                          >
                            {label}
                          </Checkbox>
                        ))}
                      </SpaceBetween>
                      <SpaceBetween size="s">
                        <Checkbox checked={false} disabled>{t("create-analysis.offline.target-databases.neptune-coming-soon")}</Checkbox>
                        <Checkbox checked={false} disabled>{t("create-analysis.offline.target-databases.keyspaces-coming-soon")}</Checkbox>
                        <Checkbox checked={false} disabled>{t("create-analysis.offline.target-databases.aurora-coming-soon")}</Checkbox>
                      </SpaceBetween>
                    </ColumnLayout>
                  </FormField>
                </Container>

                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="link" onClick={handleCancel}>
                    {t("common.actions.cancel")}
                  </Button>
                  <Button
                    variant="primary"
                    onClick={createOfflineAssessment}
                    loading={loading && uploadState === "submitting"}
                    disabled={uploadState !== "ready"}
                  >
                    {t("create-analysis.offline.start-analysis-button")}
                  </Button>
                </SpaceBetween>
              </SpaceBetween>
            )}
          </SpaceBetween>
        }
        contentType="wizard"
        navigation={
          <SideNavigation
            activeHref="/analysis/create"
            header={SideNavigationConfigurations.header}
            items={SideNavigationConfigurations.items}
          />
        }
        toolsHide
      />
    </>
  );
}
export default DatabaseAnalysisForm;
