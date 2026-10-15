"""SQL-text tools shared across collectors, detectors and parsers.

- :mod:`src.tools.sql.normalize`: the collector digest normalization step (#556).
- :mod:`src.tools.sql.facade`: the one module that calls sqlglot; table
  references and query-shape facts (joins, aggregation, subqueries, leading
  wildcards) as a small fact model, dialect from the source engine (#557,
  #796). The offline parser's ``tables_accessed``, ``has_joins``,
  ``join_count``, ``has_aggregations`` and ``has_subqueries`` come from it
  (#553, #796). It also reports each statement's access-pattern facts per
  scope (``access_facts``, #728), with the same fact definitions.
- :mod:`src.tools.sql.regex_tables` and :mod:`src.tools.sql.regex_facts`: the
  regex table extractor and the regex facts, the facade's fallback when
  sqlglot can't read a statement.
- :mod:`src.tools.sql.bulk_load`: ``COPY ... FROM`` and ``LOAD DATA`` -- the
  table they load and the fact that they are writes (#777).
- :mod:`src.tools.sql.query_facts`: the engine-neutral text facts every
  routing detector reads (#491), one detector per fact.
- :mod:`src.tools.sql.shadow`: the cross-check -- records where the facade and
  the regex disagree, per job, without changing any output.
"""
