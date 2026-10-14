"""SQL-text tools shared across collectors, detectors and parsers.

- :mod:`src.tools.sql.normalize`: the collector digest normalization step (#556).
- :mod:`src.tools.sql.facade`: the one module that calls sqlglot; table
  references as a small fact model, dialect from the source engine (#557).
- :mod:`src.tools.sql.regex_tables`: the regex table extractor the offline
  parser uses today, and the facade's fallback.
- :mod:`src.tools.sql.shadow`: shadow mode -- records where the facade and the
  regex disagree, per job, without changing any output.
"""
