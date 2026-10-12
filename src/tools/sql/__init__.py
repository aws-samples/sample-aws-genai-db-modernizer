"""SQL-text tools shared across collectors, detectors and parsers.

Today this package holds :mod:`src.tools.sql.normalize`, the collector
digest normalization step (#556). It is also where the sqlglot facade for
table references lands later (#557), once normalization has a caller.
"""
