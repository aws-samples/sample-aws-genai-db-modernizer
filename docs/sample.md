# Sample report

This is one example of what the deliverables look like, for the `wordpress` sample database used throughout [Get started](get-started.md). Your own database will produce different numbers, a different engine mix, and different risks. This page exists so you can see the shape of the output before you run anything.

## How this sample was produced

This sample runs the pipeline deterministically (`--llm-mode none`): no model call, no AWS credentials, the same way the help site's build (and `make e2e`) produces it. That changes two things compared to a real `/modernize` run in Claude Code:

- **Schema Design is skipped.** It is the one phase that always needs a model (see [Understand the results](understand-the-results.md)), so this sample has no concrete per-engine schemas (DynamoDB table definitions, DocumentDB collections, OpenSearch mappings).
- **The executive summary is deterministic text, not model-written.** A real run has Claude write the narrative; here it is assembled from the same facts with fixed wording.

Everything else makes the same decisions it would for you: Collect, Triage, Analyze, Assign, Reality Check, the ranking, the TCO, the risk list. All of it runs on the same `wordpress` collection shipped in `docs/examples/wordpress`.

## The deliverables

<ul id="sample-deliverables">
  <li><a href="decision-report.html">Decision report (HTML)</a>: the top-level recommendation, ranking, and reasoning, for someone deciding whether to proceed.</li>
  <li><a href="engineering-report.html">Engineering report (HTML)</a> (also available as <a href="engineering-report.md">Markdown</a>): the same facts with implementation detail, for the team that will build it, including migration waves, per-engine query placement, and risks (a real run also adds the per-engine schemas).</li>
  <li><a href="analysis-report.html">Interactive analysis report (HTML)</a>: per-engine analysis detail, including the path of each query through the context graph.</li>
  <li><a href="executive-summary.pdf">Executive summary (PDF)</a>: a short, stakeholder-facing version of the decision report.</li>
</ul>

All four are rendered from the same underlying report, so a fact in one matches the same fact in every other.

!!! info "Building this page locally"
    The links above resolve only after both of these have run, in order:

    ```bash
    uv run mkdocs build --strict
    uv run python scripts/build_docs_sample.py --site-dir site
    ```

    The first command renders this page from `docs/sample.md`; the second runs the deterministic pipeline against the `wordpress` sample and copies its deliverables into `site/sample/` next to it, without touching this page.
