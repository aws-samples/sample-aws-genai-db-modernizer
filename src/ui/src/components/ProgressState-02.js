import { useMemo, useState, useEffect } from 'react';
import StatusIndicator from "@cloudscape-design/components/status-indicator";

const getTheme = () => (localStorage.getItem('dbm-theme') || 'dark') === 'light' ? 'light' : 'dark';

const COLORS = {
  dark: {
    lineBg: '#414d5c',
    lineFill: '#0972D3',
    dotBorder: '#0f1b2a',
    dotInactive: '#414d5c',
    labelActive: '#ffffff',
    labelInactive: '#879596',
    labelCurrent: '#89bcf0',
    humanActive: '#ffffff',
    humanInactive: '#687078',
  },
  light: {
    lineBg: '#d5dbdb',
    lineFill: '#0972D3',
    dotBorder: '#ffffff',
    dotInactive: '#d5dbdb',
    labelActive: '#0f1b2a',
    labelInactive: '#5f6b7a',
    labelCurrent: '#0972D3',
    humanActive: '#0f1b2a',
    humanInactive: '#5f6b7a',
  },
};

const ProgressState = ({ stepsList, stepCurrent, humanStages, title, jobId, jobIdLabel, status, statusLine, refreshButton }) => {
  const isComplete = status === 'success';
  const currentStepIndex = useMemo(() => {
    if (isComplete) return stepsList.length; // all steps completed
    if (!stepCurrent) return -1;
    const index = stepsList.findIndex(step => step === stepCurrent);
    return index >= 0 ? index : -1;
  }, [stepsList, stepCurrent, isComplete]);

  const [theme, setTheme] = useState(getTheme);
  useEffect(() => {
    const handler = () => setTheme(getTheme());
    window.addEventListener('dbm-theme-change', handler);
    return () => window.removeEventListener('dbm-theme-change', handler);
  }, []);
  const c = COLORS[theme];

  const handleCopyId = () => {
    if (jobId) navigator.clipboard.writeText(jobId);
  };

  return (
    <div>
      {/* nosemgrep: missing-template-string-indicator */}
      <style>{`
        @keyframes dotPulse {
          0%, 100% { box-shadow: 0 0 6px rgba(9, 114, 211, 0.4); }
          50% { box-shadow: 0 0 16px rgba(9, 114, 211, 0.8); }
        }
      `}</style>

      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          {title && <span style={{ fontSize: '18px', fontWeight: 700, color: c.labelActive }}>{title}</span>}
          {jobId && (
            <span
              style={{
                fontSize: '15px',
                color: c.labelInactive,
                cursor: 'pointer',
                userSelect: 'all',
              }}
              title="Click to copy job ID"
              onClick={handleCopyId}
            >
              {jobIdLabel && <span style={{ fontSize: '16px', fontWeight: 600, color: c.labelActive, marginRight: '6px' }}>{jobIdLabel}</span>}
              <span style={{ fontFamily: 'monospace', letterSpacing: '0.3px' }}>{jobId}</span>
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" style={{ marginLeft: '5px', verticalAlign: '-1px', opacity: 0.5 }}>
                <rect x="5" y="5" width="9" height="9" rx="1.5" stroke={c.labelInactive} strokeWidth="1.5" fill="none"/>
                <path d="M11 5V3.5A1.5 1.5 0 009.5 2h-6A1.5 1.5 0 002 3.5v6A1.5 1.5 0 003.5 11H5" stroke={c.labelInactive} strokeWidth="1.5" fill="none"/>
              </svg>
            </span>
          )}
          {status && (
            <StatusIndicator type={status}>
              {status === 'success' ? 'Complete' : 'In progress'}
            </StatusIndicator>
          )}
        </div>
        {refreshButton}
      </div>

      {statusLine && <div style={{ marginBottom: '12px' }}>{statusLine}</div>}

      {/* Progress bar */}
      <div style={{
        position: 'relative', width: '100%',
        paddingTop: '15px', paddingBottom: '50px',
        paddingLeft: '80px', paddingRight: '80px',
        minHeight: '90px'
      }}>
        <div style={{
          position: 'absolute', top: '15px', left: '80px', right: '80px',
          height: '6px', backgroundColor: c.lineBg, borderRadius: '3px'
        }} />

        {currentStepIndex >= 0 && (
          <div style={{
            position: 'absolute', top: '15px', left: '80px',
            width: `calc((100% - 160px) * ${Math.min(currentStepIndex, stepsList.length - 1) / (stepsList.length - 1)})`,
            height: '6px', backgroundColor: c.lineFill, borderRadius: '3px',
            transition: 'width 0.3s ease'
          }} />
        )}

        {stepsList.map((step, index) => {
          const isCompleted = currentStepIndex >= 0 && index <= currentStepIndex;
          const isCurrent = currentStepIndex >= 0 && index === currentStepIndex;
          const isHuman = humanStages?.has(step);

          return (
            <div key={index} style={{
              position: 'absolute',
              left: `calc(80px + (100% - 160px) * ${index / (stepsList.length - 1)})`,
              top: '0', transform: 'translateX(-50%)',
              display: 'flex', flexDirection: 'column', alignItems: 'center'
            }}>
              <div style={{
                width: '28px', height: '28px', borderRadius: '50%',
                backgroundColor: isCompleted ? c.lineFill : c.dotInactive,
                border: `5px solid ${c.dotBorder}`,
                transition: 'all 0.3s ease',
                marginTop: '4px', marginBottom: '8px',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                ...(isCurrent ? {
                  animation: 'dotPulse 2s ease-in-out infinite',
                  boxShadow: '0 0 8px rgba(9, 114, 211, 0.6)',
                } : {})
              }}>
                {isCompleted && !isCurrent && (
                  <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
                    <path d="M2 6l3 3 5-5.5" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                )}
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                {isHuman && (
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" style={{ opacity: isCompleted ? 1 : 0.5 }}>
                    <circle cx="8" cy="5" r="3" stroke={isCompleted ? c.humanActive : c.humanInactive} strokeWidth="1.5" fill="none"/>
                    <path d="M2.5 14c0-3 2.5-4.5 5.5-4.5s5.5 1.5 5.5 4.5" stroke={isCompleted ? c.humanActive : c.humanInactive} strokeWidth="1.5" strokeLinecap="round" fill="none"/>
                  </svg>
                )}
                <span style={{
                  fontSize: '12px',
                  color: isCurrent ? c.labelCurrent : isCompleted ? c.labelActive : c.labelInactive,
                  fontWeight: isCurrent ? 'bold' : 'normal',
                  whiteSpace: 'nowrap',
                  ...(isCurrent ? { textShadow: '0 0 8px rgba(9, 114, 211, 0.6)' } : {})
                }}>
                  {step}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default ProgressState;
