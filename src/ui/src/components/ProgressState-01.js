import React, { useMemo } from 'react';
import Slider from "@cloudscape-design/components/slider";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";

const ProgressState = ({ stepsList, stepCurrent, title = "Pipeline Stage Progress" }) => {
  // Calculate current step index
  const currentStepIndex = useMemo(() => {
    if (!stepCurrent) return 0;
    const index = stepsList.findIndex(step => step === stepCurrent);
    return index >= 0 ? index : 0;
  }, [stepsList, stepCurrent]);

  // Value formatter to show raw stage names
  const valueFormatter = (value) => {
    const index = parseInt(value);
    return stepsList[index] || '';
  };

  // Create reference values for intermediate stages (all except first and last)
  const referenceValues = useMemo(() => {
    const refs = [];
    for (let i = 1; i < stepsList.length - 1; i++) {
      refs.push(i);
    }
    return refs;
  }, [stepsList]);

  return (
    <SpaceBetween size="xs">
      <Box variant="awsui-key-label">{title}</Box>
      <div className="progress-state-slider">
        <Slider
          value={currentStepIndex}
          onChange={() => {}} // No-op to make it read-only
          min={0}
          max={stepsList.length - 1}
          step={1}
          valueFormatter={valueFormatter}
          ariaDescription="Pipeline stage progress"
          referenceValues={referenceValues}
        />
      </div>
    </SpaceBetween>
  );
};

export default ProgressState;
