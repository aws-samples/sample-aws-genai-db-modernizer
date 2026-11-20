// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

//##-- React Events
import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import useLayoutAriaLabels from '../hooks/useLayoutAriaLabels';
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Wizard from "@cloudscape-design/components/wizard";
import Form from "@cloudscape-design/components/form";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import FormField from "@cloudscape-design/components/form-field";
import Select from "@cloudscape-design/components/select";
import Multiselect from "@cloudscape-design/components/multiselect";
import Input from "@cloudscape-design/components/input";
import RadioGroup from "@cloudscape-design/components/radio-group";
import Slider from "@cloudscape-design/components/slider";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Textarea from "@cloudscape-design/components/textarea";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Tiles from "@cloudscape-design/components/tiles";
import FileUpload from "@cloudscape-design/components/file-upload";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Alert from "@cloudscape-design/components/alert";

//##-- Custom Objects
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import { failureError, failureFlash } from "../utils/requestFailure";
import { isValidDatabaseName, normalizeDatabaseName } from "../utils/databaseName";
import { buildConnection } from "../utils/connectionPayload";
import { defaultTargetSelection, enginesInUseOptions, enginesInUseParam, selectedTargetEngines, targetDatabasesScope, targetEngineSelection } from "../utils/targetEngines";
import { readCollectorFile, SOURCE_ENGINE_IDS } from "../utils/collectorFile";
import { readApiLocal } from "../utils/apiRuntime";
import TargetEngineCheckboxes from "../components/TargetEngineCheckboxes";

// The source engines the upload form offers, with their display names.
const SOURCE_ENGINE_LABELS = {
  mysql: "MySQL",
  postgresql: "PostgreSQL",
  mariadb: "MariaDB",
  sqlserver: "SQL Server",
  oracle: "Oracle",
};

function SourceDatabaseSection({ formData, setFormData, errors, fieldRefs }) {
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
          ref={fieldRefs.host}
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
            ref={fieldRefs.port}
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
          constraintText={t("create-analysis.source.database-name.constraint-text")}
          description={t("create-analysis.source.database-name.description")}
          label={t("create-analysis.source.database-name.label")}
          errorText={errors.databaseName}
        >
          <Input
            ref={fieldRefs.databaseName}
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
            ref={fieldRefs.secretArn}
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
            ref={fieldRefs.secretName}
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

    </SpaceBetween>
  );
}
function CollectionOptionsSection({ formData, setFormData }) {
  const { t } = useTranslation();
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
        "AWS Performance Insights: detailed query and performance metrics",
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
function TargetDatabasesSection({ formData, setFormData, errors, targetsRef }) {
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
  // #724: only the catalog's active engines; a placeholder is never turned on.
  const handleSelectAll = () => {
    setFormData({ ...formData, targetDatabases: targetEngineSelection(true) });
  };
  const handleDeselectAll = () => {
    setFormData({ ...formData, targetDatabases: targetEngineSelection(false) });
  };
  return (
    <SpaceBetween size="l">
      <FormField
        description={t("create-analysis.target-databases.description")}
        label={t("create-analysis.target-databases.label")}
        stretch
        errorText={errors.targetDatabases}
      >
        <TargetEngineCheckboxes
          ref={targetsRef}
          selection={formData.targetDatabases}
          onChange={handleCheckboxChange}
        />
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
// The online form's step-1 fields, in the order focus looks for the first invalid one.
const SOURCE_FIELDS = ["host", "port", "databaseName", "secretArn", "secretName"];

function validateSource(formData, t) {
  const errors = {};
  if (!formData.host.trim()) {
    errors.host = t("create-analysis.source.host.error-required");
  }
  if (!formData.port.trim()) {
    errors.port = t("create-analysis.source.port.error-required");
  } else if (isNaN(formData.port) || parseInt(formData.port) < 1 || parseInt(formData.port) > 65535) {
    errors.port = t("create-analysis.source.port.error-text");
  }
  if (!formData.databaseName.trim()) {
    errors.databaseName = t("create-analysis.source.database-name.error-required");
  } else if (!isValidDatabaseName(formData.databaseName)) {
    errors.databaseName = t("create-analysis.source.database-name.error-text");
  }
  if (formData.credentialsType === "secrets-arn") {
    if (!formData.secretArn.trim()) {
      errors.secretArn = t("create-analysis.source.secret-arn.error-required");
    } else if (!formData.secretArn.startsWith("arn:aws:secretsmanager:")) {
      errors.secretArn = t("create-analysis.source.secret-arn.error-text");
    }
  } else if (formData.credentialsType === "secrets-name" && !formData.secretName.trim()) {
    errors.secretName = t("create-analysis.source.secret-name.error-required");
  }
  return errors;
}

function validateTargets(selection, t) {
  return selectedTargetEngines(selection).length > 0
    ? {}
    : { targetDatabases: t("create-analysis.target-databases.error-required") };
}

function validateAdvanced(formData, t) {
  if (!formData.collectionInterval.trim()) {
    return {};
  }
  const interval = parseInt(formData.collectionInterval);
  return isNaN(interval) || interval < 1 || interval > 1440
    ? { collectionInterval: t("create-analysis.advanced.collection-interval.error-text") }
    : {};
}

// A failed request as one sentence for the form (#2346): what failed, then the fixed
// message for the kind of failure. Only a refused create may show the API's own reason,
// and only when it is a safe sentence; the detail goes to the console.
function failureText(t, source, headerKey, context, { refusal = false } = {}) {
  const { header, content } = failureFlash(t, source, { header: t(headerKey), context, allowReason: refusal, variant: 'form' });
  return `${header}. ${content}`;
}

// PUT the file to the upload URL the API prepared.
function putFile(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url, true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(failureError(xhr.status, 'upload-file')));
    xhr.onerror = () => reject(failureError('unreachable', 'upload-file'));
    xhr.ontimeout = () => reject(failureError('timeout', 'upload-file'));
    xhr.timeout = 300000; // 5 minutes
    xhr.send(file);
  });
}

/**
 * The upload form (#2327): one Cloudscape Form, one primary action that uploads the
 * file and starts the assessment. Errors are inline, focus goes to the first invalid
 * field, and an API failure shows one plain sentence (no URL, no status code).
 */
function UploadAssessmentForm({ local, onCancel }) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [files, setFiles] = useState([]);
  const [databaseName, setDatabaseName] = useState("");
  const [nameEdited, setNameEdited] = useState(false);
  const detectOption = useMemo(
    () => ({ label: t("create-analysis.offline.database-info.source-type-detect"), value: "auto" }),
    [t],
  );
  const [sourceEngine, setSourceEngine] = useState(detectOption);
  const [engineEdited, setEngineEdited] = useState(false);
  const [enginesInUse, setEnginesInUse] = useState([]); // #1071
  const [targets, setTargets] = useState(defaultTargetSelection);
  const [fileFacts, setFileFacts] = useState(null); // { databaseName, sourceEngine } read from the file
  const [errors, setErrors] = useState({});
  const [submitError, setSubmitError] = useState("");
  const [stage, setStage] = useState("idle"); // idle, uploading, starting
  const [progress, setProgress] = useState(0);

  const fileRef = useRef(null);
  const nameRef = useRef(null);
  const targetsRef = useRef(null);
  const readToken = useRef(0);
  const inFlight = useRef(false);

  const engineOption = useCallback((id, fromFile) => ({
    label: fromFile
      ? t("create-analysis.upload.source-engine.from-file", { engine: SOURCE_ENGINE_LABELS[id] })
      : SOURCE_ENGINE_LABELS[id],
    value: id,
  }), [t]);

  const engineOptions = useMemo(() => [
    detectOption,
    ...SOURCE_ENGINE_IDS.map((id) => engineOption(id, fileFacts?.sourceEngine === id)),
  ], [detectOption, engineOption, fileFacts]);

  // Fill the name and engine from the chosen file, unless the user already set them.
  const handleFiles = useCallback(async (chosen) => {
    setFiles(chosen);
    setErrors((prev) => ({ ...prev, file: undefined }));
    setFileFacts(null);
    const token = ++readToken.current;
    if (!chosen.length) {
      return;
    }
    const facts = await readCollectorFile(chosen[0]);
    if (token !== readToken.current) {
      return; // another file was chosen meanwhile
    }
    setFileFacts(facts);
    if (facts.databaseName && !nameEdited) {
      setDatabaseName(facts.databaseName);
      setErrors((prev) => ({ ...prev, databaseName: undefined }));
    }
    if (!engineEdited) {
      setSourceEngine(SOURCE_ENGINE_IDS.includes(facts.sourceEngine) ? engineOption(facts.sourceEngine, true) : detectOption);
    }
  }, [nameEdited, engineEdited, engineOption, detectOption]);

  const validate = () => {
    const found = {};
    if (!files.length) {
      found.file = t("create-analysis.upload.file.error-required");
    }
    if (!databaseName.trim()) {
      found.databaseName = t("create-analysis.source.database-name.error-required");
    } else if (!isValidDatabaseName(databaseName)) {
      found.databaseName = t("create-analysis.source.database-name.error-text");
    }
    Object.assign(found, validateTargets(targets, t));
    setErrors(found);
    const first = [["file", fileRef], ["databaseName", nameRef], ["targetDatabases", targetsRef]]
      .find(([key]) => found[key]);
    if (first) {
      first[1].current?.focus();
    }
    return !first;
  };

  const start = async () => {
    // One start at a time: a second submit while one is in flight does nothing.
    if (inFlight.current) {
      return;
    }
    setSubmitError("");
    if (!validate()) {
      return;
    }
    inFlight.current = true;
    try {
      await uploadAndStart();
    } finally {
      inFlight.current = false;
    }
  };

  const uploadAndStart = async () => {
    const apiManager = new ApiManager();
    setStage("uploading");
    setProgress(0);
    let prepared;
    try {
      const prepare = (await apiManager.execute([{
        id: 'prepare',
        path: 'assessments/prepare',
        method: 'POST',
        params: { database_name: normalizeDatabaseName(databaseName), source_database_type: sourceEngine.value },
      }])).prepare;
      if (!prepare?.success) {
        throw failureError(prepare, 'prepare-upload');
      }
      prepared = prepare;
      await putFile(prepared.upload_url, files[0], setProgress);
      const confirm = (await apiManager.execute([{
        id: 'confirm',
        path: `assessments/${encodeURIComponent(prepared.job_id)}/uploads/confirm?database_name=${encodeURIComponent(normalizeDatabaseName(databaseName))}`,
        method: 'POST',
        params: {},
      }])).confirm;
      if (!confirm?.success) {
        throw failureError(confirm, 'confirm-upload');
      }
    } catch (error) {
      setStage("idle");
      setSubmitError(failureText(t, error, "create-analysis.error.upload-failed", 'upload'));
      return;
    }

    setStage("starting");
    try {
      const created = (await apiManager.execute([{
        id: 'create-assessment',
        path: 'assessments',
        method: 'POST',
        params: {
          job_id: prepared.job_id,
          source_database_type: sourceEngine.value,
          database_name: normalizeDatabaseName(databaseName),
          collection_mode: "offline",
          offline_s3_key: prepared.upload_key,
          // #1328: the engine scope the analysis applies (null when every engine is ticked).
          target_databases: targetDatabasesScope(targets),
          engines_in_use: enginesInUseParam(enginesInUse),
          full_analysis: false,
        },
      }]))['create-assessment'];
      if (!created?.success) {
        setStage("idle");
        setSubmitError(failureText(t, created, "create-analysis.error.start-failed", 'create-offline-assessment', { refusal: true }));
        return;
      }
      navigate(`/analysis/monitor/summary/${created.job_id}`);
    } catch (error) {
      setStage("idle");
      setSubmitError(failureText(t, error, "create-analysis.error.start-failed", 'create-offline-assessment'));
    }
  };

  const busy = stage !== "idle";
  const factsText = () => {
    const engine = fileFacts?.sourceEngine && SOURCE_ENGINE_LABELS[fileFacts.sourceEngine];
    const name = fileFacts?.databaseName;
    if (engine && name) {
      return t("create-analysis.upload.file.read-name-engine", { engine, name });
    }
    if (name) {
      return t("create-analysis.upload.file.read-name", { name });
    }
    if (engine) {
      return t("create-analysis.upload.file.read-engine", { engine });
    }
    return null;
  };
  const readText = fileFacts ? factsText() : null;

  return (
    <form onSubmit={(event) => { event.preventDefault(); start(); }} noValidate>
      <Form
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button formAction="none" variant="link" onClick={onCancel} disabled={busy}>
              {t("common.actions.cancel")}
            </Button>
            <Button variant="primary" loading={busy}>
              {t("create-analysis.upload.start-button")}
            </Button>
          </SpaceBetween>
        }
        errorText={submitError || undefined}
        errorIconAriaLabel={t("create-analysis.offline.upload.error-icon-aria-label")}
      >
        <SpaceBetween size="l">
          {/* Until the local API can receive the file (#2408); remove with that fix. */}
          {local === true && (
            <Alert type="info">{t("create-analysis.upload.local-start-unavailable")}</Alert>
          )}
          <Container header={<Header variant="h2">{t("create-analysis.upload.file.title")}</Header>}>
            <SpaceBetween size="m">
              <FormField
                label={t("create-analysis.upload.file.label")}
                description={t({
                  true: "create-analysis.upload.file.description-local",
                  false: "create-analysis.upload.file.description-deployed",
                }[String(local)] || "create-analysis.upload.file.description-unknown")}
                constraintText={t("create-analysis.upload.file.constraint-text")}
                errorText={errors.file}
              >
                <FileUpload
                  ref={fileRef}
                  onChange={({ detail }) => {
                    // The request in flight uses the file chosen when it started.
                    if (!inFlight.current) {
                      handleFiles(detail.value);
                    }
                  }}
                  value={files}
                  i18nStrings={{
                    uploadButtonText: (multiple) => (multiple ? t("create-analysis.offline.upload.choose-files") : t("create-analysis.offline.upload.choose-file")),
                    dropzoneText: (multiple) => (multiple ? t("create-analysis.offline.upload.drop-files") : t("create-analysis.offline.upload.drop-file")),
                    removeFileAriaLabel: (index) => t("create-analysis.offline.upload.remove-file", { index: index + 1 }),
                    limitShowFewer: t("create-analysis.offline.upload.show-fewer-files"),
                    limitShowMore: t("create-analysis.offline.upload.show-more-files"),
                    errorIconAriaLabel: t("create-analysis.offline.upload.error-icon-aria-label"),
                  }}
                  accept=".json,application/json"
                  showFileSize
                  showFileLastModified
                  tokenLimit={1}
                />
              </FormField>
              {readText && (
                <StatusIndicator type="success">{readText}</StatusIndicator>
              )}
              {fileFacts && !readText && (
                <StatusIndicator type="info">{t("create-analysis.upload.file.read-none")}</StatusIndicator>
              )}
              {busy && (
                <ProgressBar
                  value={stage === "uploading" ? progress : 100}
                  label={t("create-analysis.offline.upload.progress-label")}
                  description={t(stage === "uploading"
                    ? "create-analysis.upload.progress.uploading"
                    : "create-analysis.upload.progress.starting")}
                />
              )}
            </SpaceBetween>
          </Container>

          <Container header={<Header variant="h2">{t("create-analysis.upload.database.title")}</Header>}>
            <SpaceBetween size="l">
              <ColumnLayout columns={2}>
                <FormField
                  label={t("create-analysis.source.database-name.label")}
                  description={t("create-analysis.upload.database.name-description")}
                  constraintText={t("create-analysis.upload.database.name-constraint-text")}
                  errorText={errors.databaseName}
                >
                  <Input
                    ref={nameRef}
                    value={databaseName}
                    disabled={busy}
                    onChange={({ detail }) => {
                      setDatabaseName(detail.value);
                      setNameEdited(true);
                      setErrors((prev) => ({ ...prev, databaseName: undefined }));
                    }}
                  />
                </FormField>
                <FormField
                  label={t("create-analysis.upload.source-engine.label")}
                  description={t("create-analysis.offline.database-info.source-type-description")}
                >
                  <Select
                    selectedOption={sourceEngine}
                    options={engineOptions}
                    disabled={busy}
                    onChange={({ detail }) => {
                      setSourceEngine(detail.selectedOption);
                      setEngineEdited(true);
                    }}
                  />
                </FormField>
              </ColumnLayout>
              <FormField
                label={t("create-analysis.offline.database-info.engines-in-use-label")}
                description={t("create-analysis.offline.database-info.engines-in-use-description")}
              >
                <Multiselect
                  selectedOptions={enginesInUse}
                  onChange={({ detail }) => setEnginesInUse(detail.selectedOptions)}
                  options={enginesInUseOptions()}
                  placeholder={t("create-analysis.offline.database-info.engines-in-use-placeholder")}
                  disabled={busy}
                />
              </FormField>
            </SpaceBetween>
          </Container>

          <Container
            header={
              <Header variant="h2" description={t("create-analysis.upload.targets.description")}>
                {t("create-analysis.upload.targets.title")}
              </Header>
            }
          >
            <FormField
              label={t("create-analysis.upload.targets.label")}
              errorText={errors.targetDatabases}
              stretch
            >
              <TargetEngineCheckboxes
                ref={targetsRef}
                disabled={busy}
                selection={targets}
                onChange={(key, checked) => {
                  setTargets((prev) => ({ ...prev, [key]: checked }));
                  setErrors((prev) => ({ ...prev, targetDatabases: undefined }));
                }}
              />
            </FormField>
          </Container>
        </SpaceBetween>
      </Form>
    </form>
  );
}

/**
 * The live-database form, a Wizard, offered only where the API is not local (#2327).
 */
function OnlineAssessmentWizard({ onCancel }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
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
    // Collection options
    queryLogPeriod: 7,
    queryLogSource: {
      label: "Performance Insights (recommended)",
      tags: ["Recommended"],
      value: "performance-insights",
    },
    // Target databases
    targetDatabases: defaultTargetSelection(),
    // Advanced options
    includeTablePatterns: "",
    excludeTablePatterns: "",
    topSqlQueries: "100",
    topWaitEvents: "50",
    customQueryPatterns: "",
    collectionInterval: "",
  });
  const [errors, setErrors] = useState({});
  const [submitError, setSubmitError] = useState("");
  const [loading, setLoading] = useState(false);

  const host = useRef(null);
  const port = useRef(null);
  const databaseName = useRef(null);
  const secretArn = useRef(null);
  const secretName = useRef(null);
  const targetsRef = useRef(null);
  const fieldRefs = useMemo(() => ({ host, port, databaseName, secretArn, secretName }), []);

  const errorsFor = useCallback((stepIndex) => {
    if (stepIndex === 0) {
      return validateSource(formData, t);
    }
    if (stepIndex === 2) {
      return validateTargets(formData.targetDatabases, t);
    }
    if (stepIndex === 3) {
      return validateAdvanced(formData, t);
    }
    return {};
  }, [formData, t]);

  const focusFirst = (found) => {
    const key = [...SOURCE_FIELDS, "targetDatabases"].find((k) => found[k]);
    const ref = key === "targetDatabases" ? targetsRef : fieldRefs[key];
    // After the step's errors render, so a field of this step has mounted.
    setTimeout(() => ref?.current?.focus(), 0);
  };

  const createAssessment = async () => {
    const found = { ...validateSource(formData, t), ...validateTargets(formData.targetDatabases, t), ...validateAdvanced(formData, t) };
    setErrors(found);
    if (Object.keys(found).length) {
      const step = Object.keys(validateSource(formData, t)).length ? 0
        : Object.keys(validateTargets(formData.targetDatabases, t)).length ? 2 : 3;
      setActiveStepIndex(step);
      focusFirst(found);
      return;
    }
    setLoading(true);
    setSubmitError("");
    try {
      const options = {
        query_log_period_days: formData.queryLogPeriod,
        query_log_source: formData.queryLogSource.value,
        top_sql_queries: parseInt(formData.topSqlQueries),
        top_wait_events: parseInt(formData.topWaitEvents),
      };
      if (formData.includeTablePatterns.trim()) {
        options.include_table_patterns = formData.includeTablePatterns.trim();
      }
      if (formData.excludeTablePatterns.trim()) {
        options.exclude_table_patterns = formData.excludeTablePatterns.trim();
      }
      const result = (await new ApiManager().execute([{
        id: 'create-assessment',
        path: 'assessments',
        method: 'POST',
        params: {
          source_database_type: "mysql", // TODO: Get from RDS instance or form
          database_name: normalizeDatabaseName(formData.databaseName),
          // Credentials go by reference only (a Secrets Manager ARN or name),
          // never a password (#723, ADR-004 Decision 5).
          connection: buildConnection(formData),
          options,
          // #1328: the engine scope the analysis applies (null when every engine is ticked).
          target_databases: targetDatabasesScope(formData.targetDatabases),
          full_analysis: false,
        },
      }]))['create-assessment'];
      if (!result?.success) {
        setSubmitError(failureText(t, result, "create-analysis.error.create-failed", 'create-assessment', { refusal: true }));
        return;
      }
      navigate(`/analysis/monitor/summary/${result.job_id}`);
    } catch (error) {
      setSubmitError(failureText(t, error, "create-analysis.error.create-failed", 'create-assessment'));
    } finally {
      setLoading(false);
    }
  };

  const handleNavigate = (detail) => {
    if (detail.requestedStepIndex > activeStepIndex) {
      const found = errorsFor(activeStepIndex);
      setErrors(found);
      if (Object.keys(found).length) {
        focusFirst(found);
        return;
      }
    }
    setActiveStepIndex(detail.requestedStepIndex);
  };

  const steps = [
    {
      title: t("create-analysis.step.source-database.title"),
      description: t("create-analysis.step.source-database.description"),
      content: <SourceDatabaseSection formData={formData} setFormData={setFormData} errors={errors} fieldRefs={fieldRefs} />,
    },
    {
      title: t("create-analysis.step.collection-options.title"),
      description: t("create-analysis.step.collection-options.description"),
      content: <CollectionOptionsSection formData={formData} setFormData={setFormData} />,
    },
    {
      title: t("create-analysis.step.target-databases.title"),
      description: t("create-analysis.step.target-databases.description"),
      content: <TargetDatabasesSection formData={formData} setFormData={setFormData} errors={errors} targetsRef={targetsRef} />,
    },
    {
      title: t("create-analysis.step.advanced-options.title"),
      description: t("create-analysis.step.advanced-options.description"),
      content: <AdvancedOptionsSection formData={formData} setFormData={setFormData} errors={errors} />,
      isOptional: true,
      errorText: submitError || undefined,
    },
  ];

  return (
    <Wizard
      i18nStrings={{
        stepNumberLabel: (stepNumber) => t("create-analysis.wizard.step-number-label", { stepNumber }),
        collapsedStepsLabel: (stepNumber, stepsCount) => t("create-analysis.wizard.collapsed-steps-label", { stepNumber, stepsCount }),
        skipToButtonLabel: (step) => t("create-analysis.wizard.skip-to-button-label", { title: step.title }),
        navigationAriaLabel: t("create-analysis.wizard.navigation-aria-label"),
        cancelButton: t("common.actions.cancel"),
        previousButton: t("create-analysis.wizard.previous-button"),
        nextButton: t("create-analysis.wizard.next-button"),
        submitButton: t("create-analysis.upload.start-button"),
        optional: t("create-analysis.wizard.optional"),
      }}
      onNavigate={({ detail }) => handleNavigate(detail)}
      onCancel={onCancel}
      onSubmit={createAssessment}
      activeStepIndex={activeStepIndex}
      steps={steps}
      isLoadingNextStep={loading}
    />
  );
}

function DatabaseAnalysisForm() {
  const { t } = useTranslation();
  const layoutAriaLabels = useLayoutAriaLabels();
  const navigate = useNavigate();

  // #2327: true when the API keeps uploads on this machine; null until it answers.
  const [apiLocal, setApiLocal] = useState(null);
  const [mode, setMode] = useState("upload");
  const [modeChosen, setModeChosen] = useState(false);

  useEffect(() => {
    let live = true;
    readApiLocal(ApiConfigurations.baseUrl).then((local) => {
      if (live) {
        setApiLocal(local);
      }
    });
    return () => { live = false; };
  }, []);

  // A deployment that is not local opens on the live-database form, until the user picks.
  useEffect(() => {
    if (apiLocal === false && !modeChosen) {
      setMode("online");
    }
  }, [apiLocal, modeChosen]);

  const deployed = apiLocal === false;
  const handleCancel = useCallback(() => navigate('/dashboard'), [navigate]);

  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        ariaLabels={layoutAriaLabels}
        breadcrumbs={
          <BreadcrumbGroup
            items={[
              { href: "/dashboard", text: t("common.breadcrumb.assessments") },
              { href: "#", text: t("create-analysis.breadcrumb.create-analysis") },
            ]}
          />
        }
        content={
          <SpaceBetween size="l">
            <Header
              variant="h1"
              description={t(deployed ? "create-analysis.header.description-deployed" : "create-analysis.header.description")}
            >
              {t("create-analysis.header.title")}
            </Header>

            {deployed && (
              <FormField label={t("create-analysis.form.analysis-mode.label")} stretch>
                <Tiles
                  items={[
                    {
                      value: "upload",
                      label: t("create-analysis.form.analysis-mode.offline-label"),
                      description: t("create-analysis.form.analysis-mode.offline-description"),
                    },
                    {
                      value: "online",
                      label: t("create-analysis.form.analysis-mode.online-label"),
                      description: t("create-analysis.form.analysis-mode.online-description"),
                    },
                  ]}
                  value={mode}
                  onChange={({ detail }) => {
                    setMode(detail.value);
                    setModeChosen(true);
                  }}
                />
              </FormField>
            )}

            {deployed && mode === "online"
              ? <OnlineAssessmentWizard onCancel={handleCancel} />
              : <UploadAssessmentForm local={apiLocal} onCancel={handleCancel} />}
          </SpaceBetween>
        }
        contentType="form"
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
