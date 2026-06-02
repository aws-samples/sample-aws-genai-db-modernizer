import React, { useState } from 'react';
import TopNavigation from '@cloudscape-design/components/top-navigation';
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import { LayoutConfigurations } from '../config/GlobalConfigurations';

function AppHeader() {
  const [isDark, setIsDark] = useState(
    (localStorage.getItem('dbm-theme') || 'dark') === 'dark'
  );

  const toggleTheme = () => {
    const newTheme = isDark ? 'light' : 'dark';
    localStorage.setItem('dbm-theme', newTheme);
    applyMode(newTheme === 'light' ? Mode.Light : Mode.Dark);
    setIsDark(!isDark);
    // Notify components that read theme from localStorage (e.g. ProgressState)
    window.dispatchEvent(new Event('dbm-theme-change'));
  };

  return (
    <TopNavigation
      identity={{
        href: '/',
        title: LayoutConfigurations['application-title'],
      }}
      utilities={[
        {
          type: 'button',
          iconName: isDark ? 'star' : 'star-filled',
          title: isDark ? 'Switch to light theme' : 'Switch to dark theme',
          onClick: toggleTheme,
        },
      ]}
    />
  );
}

export default AppHeader;
