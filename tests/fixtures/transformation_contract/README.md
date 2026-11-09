# Transformation contract golden fixtures

A golden fixture is one JSON file: a source `dialect`, a target `engine`, a `seed`, trimmed collector `tables`, the contract `rules` to run, an `input_sha256`, and the `expected` output rows, cast failures and unchecked columns per source table.

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

A column is unchecked, and absent from the cast failures, when no DuckDB type answers like the target engine (for example a bare PostgreSQL `numeric`) or when the generator does not model the source column type. A backend that cannot check a type must do the same and report the column, not guess. Each source table's `unchecked` list names those target columns in rule order, and every backend must report exactly that list (`text_lengths_postgresql`: a PostgreSQL `interval` source column).

A boolean source column (PostgreSQL `boolean`) is fed to an `aurora_mysql` target as `1` or `0`, the way a boolean reaches a MySQL `TINYINT` (`pg_boolean_into_mysql`).

## Expected output convention

Every expected output value is its canonical text form, and null stays null. The harness of every backend applies this to its own output before comparing:

- integers and text: `str(value)`
- decimals: `str(Decimal)`, keeping the scale (`90.91`)
- dates: `YYYY-MM-DD`
- timestamps: `YYYY-MM-DDTHH:MM:SS`
- booleans: `true` or `false`
- floats: the shortest decimal that reads back as the same value at the column's precision, in plain digits (never an exponent) with at least one digit after the point (`-0.01`, `100000.0`); `NaN`, `Infinity` and `-Infinity` as written. The precision is 32 bits for a single-precision column (MySQL `FLOAT`, `FLOAT(p)` with p up to 24, `FLOAT(M,D)`; PostgreSQL `real`, `float4`, `float(p)` with p up to 24) and 64 bits otherwise, so a `FLOAT(7,2)` value of 36699.99 is `36699.99`, not its 64-bit expansion (`float_mysql_rounds_to_scale`)
- bytes: `\x` followed by the bytes in lower-case hex (`\x616200`)

A cast failure is `[target column, source value as text, target type]`. The value is always the source text, never a JSON number (the unsigned bigint maximum exceeds 2^63).

## Engine pairs

The `boolean_*` pair uses one table and one rule on two engines: a `tinyint(1)` value of 2 converts to `boolean` on `aurora_mysql` (kept as 2) and is a cast failure on `aurora_postgresql`.

## Target checks beyond the type

The check models assignment, an INSERT into the target column in MySQL strict mode or PostgreSQL, not an explicit `CAST` (PostgreSQL's `CAST(x AS varchar(7))` cuts silently instead of failing). MySQL text columns are assumed to be `utf8mb4`: a `utf8mb3` or `latin1` column would reject the four-byte edge values that pass here.

A conversion also fails when the target engine would reject the converted value (#1830):

- A MySQL `UNSIGNED` decimal or float rejects every negative source value, including one that rounds to zero (`-0.001` into `decimal(6,2) unsigned`), because MySQL tests the sign before it rounds (`unsigned_decimal_mysql_rejects_negative`).
- `CHAR(n)` and `VARCHAR(n)` hold `n` characters, not bytes (`text_lengths_postgresql`: four four-byte characters fit `character(6)`). Excess trailing spaces are cut rather than rejected. A PostgreSQL `CHAR(n)` comes back padded with spaces (`text_lengths_postgresql`); a MySQL one comes back without trailing spaces (unit tests only: no fixture runs a MySQL `CHAR` or feeds excess trailing spaces yet).
- MySQL `BINARY(n)` and `VARBINARY(n)` hold `n` bytes of the text's UTF-8. A `BINARY(n)` value, source or target, is padded with zero bytes to `n`, as MySQL stores it, so the generator gives a `binary(4)` source `ab` as 4 bytes (`bytes_and_enum_mysql`).
- A PostgreSQL `bytea` source is hex text (`\x0a`); it is decoded to its bytes before a byte target checks or holds it (`pg_bytea_into_mysql`).
- MySQL `FLOAT(M,D)` is single precision whatever `M`, `DOUBLE(M,D)` double. Both round as MySQL does, `floor(x)` plus the fraction times `10^D` rounded half to even, divided by `10^D` (so `-0.005` becomes `0.00`), and a value above `10^(M-D) - 10^-D` in magnitude fails (`float_mysql_rounds_to_scale`).

## Known gap

PostgreSQL `infinity` dates have no portable golden yet: DuckDB maps them to `9999-12-31` and `0001-01-01`.
