// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { forwardRef } from 'react';
import { useTranslation } from 'react-i18next';
import Checkbox from '@cloudscape-design/components/checkbox';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import { ENGINE_LABELS } from '../utils/engineCatalog.generated';
import { SELECTABLE_TARGET_ENGINES } from '../utils/targetEngines';

/**
 * The target-engine checkboxes, one per active engine of the catalog (#724). A future
 * placeholder is not offered (#2327: no "Coming soon" boxes). Each carries the locale
 * description `create-analysis.target-databases.<engine id>-description`. The ref
 * focuses the first checkbox, for a form that moves focus to its first invalid field.
 */
const TargetEngineCheckboxes = forwardRef(function TargetEngineCheckboxes({ selection, onChange, disabled }, ref) {
  const { t } = useTranslation();
  return (
    <ColumnLayout columns={2}>
      {SELECTABLE_TARGET_ENGINES.map((id, index) => (
        <Checkbox
          key={id}
          ref={index === 0 ? ref : undefined}
          checked={selection?.[id] === true}
          disabled={disabled}
          onChange={({ detail }) => onChange(id, detail.checked)}
          description={t(`create-analysis.target-databases.${id}-description`)}
        >
          {ENGINE_LABELS[id]}
        </Checkbox>
      ))}
    </ColumnLayout>
  );
});

export default TargetEngineCheckboxes;
