import { render } from "react-dom";
import { BrowserRouter } from "react-router-dom";

// Cloudscape Styles
import '@cloudscape-design/global-styles/index.css';
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import './styles/global.css';

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

// Theme setup - read from localStorage, default to dark
const savedTheme = localStorage.getItem('dbm-theme') || 'dark';
applyMode(savedTheme === 'light' ? Mode.Light : Mode.Dark);

function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  );
}

render(<App />, document.getElementById("root"));
