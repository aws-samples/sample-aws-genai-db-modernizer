"""SQL-text tools shared across collectors, detectors and parsers.

- :mod:`src.tools.sql.normalize`: the collector digest normalization step (#556).
- :mod:`src.tools.sql.facade`: the one module that calls sqlglot; table
  references as a small fact model, dialect from the source engine (#557).
  The offline parser's ``tables_accessed`` comes from it (#553).
- :mod:`src.tools.sql.regex_tables`: the regex table extractor, the facade's
  fallback when sqlglot can't read a statement.
- :mod:`src.tools.sql.bulk_load`: ``COPY ... FROM`` and ``LOAD DATA`` -- the
  table they load and the fact that they are writes (#777).
- :mod:`src.tools.sql.shadow`: the cross-check -- records where the facade and
  the regex disagree, per job, without changing any output.
"""
