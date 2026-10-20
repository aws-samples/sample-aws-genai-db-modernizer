import { useTranslation } from 'react-i18next';
import Checkbox from '@cloudscape-design/components/checkbox';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { ENGINE_LABELS } from '../utils/engineCatalog.generated';
import { COMING_SOON_TARGET_ENGINES, SELECTABLE_TARGET_ENGINES } from '../utils/targetEngines';

/**
 * The target-database checkboxes, from the engine catalog (#724): one enabled checkbox
 * per active engine and one disabled "Coming soon" checkbox per future placeholder.
 * `descriptionPrefix` names the locale keys `<prefix>.<engine id>-description`; without
 * one the checkboxes carry no description (the offline form).
 */
export default function TargetEngineCheckboxes({ selection, onChange, descriptionPrefix }) {
  const { t } = useTranslation();
  const describe = (id) => (descriptionPrefix ? t(`${descriptionPrefix}.${id}-description`) : undefined);
  return (
    <ColumnLayout columns={2}>
      <SpaceBetween size="m">
        {SELECTABLE_TARGET_ENGINES.map((id) => (
          <Checkbox
            key={id}
            checked={selection?.[id] === true}
            onChange={({ detail }) => onChange(id, detail.checked)}
            description={describe(id)}
          >
            {ENGINE_LABELS[id]}
          </Checkbox>
        ))}
      </SpaceBetween>
      <SpaceBetween size="m">
        {COMING_SOON_TARGET_ENGINES.map((id) => (
          <Checkbox key={id} checked={false} disabled description={describe(id)}>
            {t('create-analysis.target-databases.coming-soon', { engine: ENGINE_LABELS[id] })}
          </Checkbox>
        ))}
      </SpaceBetween>
    </ColumnLayout>
  );
}
