# Transformation contract golden fixtures

A golden fixture is one JSON file: a source `dialect`, a target `engine`, a `seed`, trimmed collector `tables`, the contract `rules` to run, an `input_sha256`, and the `expected` output rows and cast failures per source table.

Every backend must pass every file here with no change to the expected block. The DuckDB backend runs them in `tests/unit/tools/transformation_contract/test_golden_fixtures.py`. A fixture holds data only, never SQL text, so another backend can read the same files.

## Inputs

The input rows are not stored. They come from `generate_rows(table, dialect=..., seed=...)`, so the generator is part of the fixture contract: another backend's harness calls it, or reads rows produced from it. `input_sha256` is the SHA-256 of the canonical JSON (sorted keys, compact separators, `default=str`, no ASCII escaping) of the generated rows of every table, keyed by table id. A generator change fails the test loudly instead of shifting the inputs.

The seeds are load-bearing. A seed picks the random values and which rows hold the edge values, so changing one changes what a fixture proves. For example, `numbers_postgresql` relies on its seed to put the `999.99` edge value in a row that overflows `numeric(4,2)`, and the `boolean_*` pair relies on seed 1 to produce a `0` row.

Inputs are fed to a backend as their text forms, which is what the check models. That is why `'2'` is a cast failure to `boolean` on `aurora_postgresql`.

The text form of an input value is exactly this, and every backend's harness must use it:

- null stays null;
- booleans: `true` or `false`;
- decimals: `format(value, "f")`, plain digits with no exponent (`0.0000002`, never `2E-7`) and the scale kept;
- everything else (integers, floats, strings, dates and times already generated as strings): `str(value)`.

Every output column is a conversion to its type: a transform to its `to_type`, a copy to its `target_type`. A non-null input whose conversion fails is a cast failure, copies included (`copy_datetime_mysql_zero_date`: a zero `datetime` copied to a `datetime` column fails on `aurora_mysql`).

A column is unchecked, and absent from the cast failures, when no DuckDB type answers like the target engine (for example a bare PostgreSQL `numeric`) or when the generator does not model the source column type. A backend that cannot check a type must do the same and report the column, not guess.

## Expected output convention

Every expected output value is its canonical text form, and null stays null. The harness of every backend applies this to its own output before comparing:

- integers and text: `str(value)`
- decimals: `str(Decimal)`, keeping the scale (`90.91`)
- dates: `YYYY-MM-DD`
- timestamps: `YYYY-MM-DDTHH:MM:SS`
- booleans: `true` or `false`

A cast failure is `[target column, source value as text, target type]`. The value is always the source text, never a JSON number (the unsigned bigint maximum exceeds 2^63).

## Engine pairs

The `boolean_*` pair uses one table and one rule on two engines: a `tinyint(1)` value of 2 converts to `boolean` on `aurora_mysql` (kept as 2) and is a cast failure on `aurora_postgresql`.

## Known gap

PostgreSQL `infinity` dates have no portable golden yet: DuckDB maps them to `9999-12-31` and `0001-01-01`.
