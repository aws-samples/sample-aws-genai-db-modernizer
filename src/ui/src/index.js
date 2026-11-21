// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { render } from "react-dom";
import { BrowserRouter } from "react-router-dom";

// Cloudscape Styles
import '@cloudscape-design/global-styles/index.css';
import './styles/global.css';
import { installThemeVars } from './styles/themeTokens';
import { installTheme } from './utils/theme';

// i18n
import './i18n';

import AppRoutes from "./AppRoutes";

// Suppress ResizeObserver errors (benign warning from Cloudscape components)
const resizeObserverErrorHandler = (e) => {
  if (e.message === 'ResizeObserver loop completed with undelivered notifications.') {
    const resizeObserverErrDiv = document.getElementById('webpack-dev-server-client-overlay-div');
    const resizeObserverErr = document.getElementById('webpack-dev-server-client-overlay');
    if (resizeObserverErr) {
      resizeObserverErr.setAttribute('style', 'display: none');
    }
    if (resizeObserverErrDiv) {
      resizeObserverErrDiv.setAttribute('style', 'display: none');
    }
  }
};
window.addEventListener('error', resizeObserverErrorHandler);

// The theme (#2490): the saved Light or Dark choice, else the OS setting, followed live.
installTheme();
// The app's --dbm-* colours, as Cloudscape design tokens (#2311).
installThemeVars();

function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  );
}

render(<App />, document.getElementById("root"));
