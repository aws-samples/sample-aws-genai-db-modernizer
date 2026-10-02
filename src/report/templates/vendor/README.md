# Vendored assets

`chart.umd.min.js` is the Chart.js 4.4.0 UMD build (npm `chart.js@4.4.0`, `dist/chart.umd.js`), kept under this name because `analysis_report._chart_js_tag` loads it by that filename. MIT licensed — see `LICENSE.chartjs.md`.

To update: `npm pack chart.js@<version>`, extract `dist/chart.umd.js`, copy it in as `chart.umd.min.js`, and refresh `LICENSE.chartjs.md` from the package's `LICENSE.md`.
