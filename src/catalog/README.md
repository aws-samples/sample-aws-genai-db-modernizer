# Rule catalog

The TOML files here are the rule catalog (ADR-031 Decision 8): one file per engine (`engines/`), per pattern type (`patterns/`), per relational-move gap rule (`gaps/`, with `aurora_versions.toml`) and per write intent (`write_intents/`), plus the shared files at this level (`routing.toml`, `gates.toml`, `triage_signals.toml`, `access_classes.toml`, `carried_writes.toml`, `prices.toml`, `model_prices.toml`). The code reads them through `src/shared/catalog/`, which also defines every field. `docs/architecture/rule-catalog.md` is the rendered view (`uv run python scripts/render_catalog.py`).

## Compatibility

- **The format is backwards compatible within 0.x.** File locations, table and key names, value types and which keys are required stay as they are. A release may add keys, files and file kinds; it does not remove, rename or retype a key, drop a required key from a file, make an optional key required, or remove a file without a documented deprecation first. An optional key may be absent from any file. `tests/contract/catalog_format_snapshot.json` records the format and `tests/contract/test_catalog_format_snapshot.py` fails on any such change. The loader also reads the files every recorded release shipped (`tests/contract/test_catalog_old_releases_load.py`).
- **Rule ids are stable.** The engine, pattern, gap rule and write intent files, and the named entries of the shared files (thresholds, capabilities, signals), keep their ids; retiring one needs a deprecation like any other removal.
- **Maps keyed by data are recorded by their value schema.** The instance classes and offer dates in `prices.toml` and the models in `model_prices.toml` come and go with the public price lists; their entries keep the same value types and fields (`DATA_KEYED` in `scripts/catalog_format_snapshot.py`).
- **The values are tuning.** Scores, penalties, prices and thresholds change between releases, and each release's notes list the values that moved ("Catalog values changed since <previous tag>"). Read the values from these files; do not hard-code them.

A deliberate removal keeps the key or file readable for at least one release, says so in the release notes, and then needs a maintainer-approved snapshot update. The snapshot is never edited by hand; the release step records it from the tag:

```bash
uv run python scripts/catalog_format_snapshot.py --write-released <tag>
```
