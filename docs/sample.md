# Sample report

This is one example of what the deliverables look like, for the `wordpress` sample database used throughout [Get started](get-started.md). Your own database will produce different numbers, a different engine mix, and different risks. This page exists so you can see the shape of the output before you run anything.

## How this sample was produced

This is a real model run of the `wordpress` sample at release v0.3.0-beta.1: one interactive `/modernize` run in Claude Code, with its approval gate, on the collection shipped in `docs/examples/wordpress`. Claude wrote the parts a model writes in every run: the reality-check review, the per-engine schema designs (Aurora MySQL, DynamoDB with its design review, ElastiCache) and the executive summary. Collect, Triage, Analyze, Assign, the ranking, the TCO and the risk list are deterministic, as they are for you.

The run's synthesis report is committed under [`docs/examples/wordpress/sample-run/`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/tree/main/docs/examples/wordpress/sample-run), and each build of this site renders the deliverables from it with the current code, so wording and layout fixes show up here without a new run. The assessment itself is the recorded one, and it is refreshed once per release.

## The deliverables

These are the files a run writes to its deliverables folder, under the same names:

<!-- deliverables-list -->
<p>Every run writes them to <code>artifacts/&lt;db&gt;/&lt;job&gt;/deliverables/</code>.</p>
<ul id="sample-deliverables">
  <li><a href="executive-report.pdf">Executive report (PDF)</a>, <code>executive-report.pdf</code>: the stakeholder-facing landscape briefing: recommendation, the numbers, why, migration waves, risks, decisions needed and next steps.</li>
  <li><a href="engineering-report.html">Engineering report (HTML)</a> (the run writes it as <a href="engineering-report.md">Markdown</a>, <code>engineering-report.md</code>): the same facts with implementation detail, for the team that will build it, including the per-engine schemas, migration waves, per-engine query placement, and risks.</li>
  <li><a href="assessment-data.json">Assessment Data (JSON)</a>, <code>assessment-data.json</code>: the complete machine-readable assessment the other files are rendered from.</li>
  <li><a href="index.md">Folder index</a>, <code>index.md</code>: each file, who it is for, and the assignment version.</li>
</ul>
<!-- /deliverables-list -->

All of them are rendered from the same underlying report, so a fact in one matches the same fact in every other. A real run also opens the local UI, whose results page shows the same report interactively, with each query's path and the reason for its engine.

!!! info "Building this page locally"
    The links above resolve only after both of these have run, in order:

    ```bash
    uv run mkdocs build --strict
    uv run python scripts/build_docs_sample.py --site-dir site
    ```

    The first command renders this page from `docs/sample.md`; the second renders the recorded `wordpress` run's deliverables with the current code and copies them into `site/sample/` next to it, without touching this page. Add `--deterministic` to the second command to render a fresh deterministic run instead (no model: Schema Design is skipped and the executive summary uses fixed wording).
