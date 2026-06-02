import { memo } from 'react';
import Icon from "@cloudscape-design/components/icon";
import './SectionSeparator.css';

const SectionSeparator = memo(({ title, description, onTopClick }) => {
  return (
    <div className="section-separator">
      <div className="section-separator-content">
        <div className="section-separator-text">
          <div className="section-separator-title">{title}</div>
          {description && (
            <div className="section-separator-description">{description}</div>
          )}
        </div>
        <div className="section-separator-top">
          <button
            className="section-separator-top-link"
            onClick={onTopClick}
            type="button"
            aria-label="Scroll to top"
          >
            <Icon name="angle-up" size="medium" />
          </button>
        </div>
      </div>
    </div>
  );
});

SectionSeparator.displayName = 'SectionSeparator';

export default SectionSeparator;
