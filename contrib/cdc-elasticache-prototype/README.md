# CDC-to-ElastiCache prototype (quarantined snapshot)

This directory is a snapshot of a change data capture (CDC) prototype that streams changes from Aurora or RDS (MySQL or PostgreSQL) into Amazon ElastiCache for Valkey: Debezium Server, a Valkey Streams broker, a Go sink, a parallel bulk loader with a load-to-CDC handoff, CloudFormation templates, a demo and benchmark write-ups. It is here to bootstrap the data movement work tracked in [#1412](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1412), and it is ported into the product piece by piece, tests first, through that issue's sub-issues.

**Status: quarantined.** Nothing in this directory was built, run, imported, tested or deployed when it was imported, and the repository does not build, import, test, lint, type-check or package it: the format, lint, type and schema hooks, Ruff, Black, isort, mypy, Bandit, Semgrep, pytest and the sdist all skip `contrib/`. Secret scanning still covers it. [`tests/unit/test_contrib_quarantine.py`](../../tests/unit/test_contrib_quarantine.py) fails if anything that runs (`src/`, `scripts/`, `ci/`, `infrastructure/`, `pipeline/`, the workflows, the Claude Code commands or the `Makefile`) imports from or points at this directory (Python docstrings and comments may credit it). Do not deploy it as is: it carries the prototype shortcuts listed in #1412, which the port fixes rather than copies.

The Go modules and `requirements.txt` files here are visible to GitHub's dependency graph, and some pins are old, so Dependabot may raise alerts on them. Nothing here ships or runs, so such alerts do not affect the product: they are dismissed as quarantined code, and the dependency is chosen afresh when the piece that uses it is ported.

## Authors

Written by Norbert Funke ([@nfunke](https://github.com/nfunke)) and Dinesh Kumar ([@happycontribute](https://github.com/happycontribute)), who agreed to contribute it to this repository under its licence.

Norbert Funke started the project and wrote its first Kafka sink (`legacy-kafka-sink/valkey_default.py`), which the later Python sinks build on. Dinesh Kumar built the Debezium Server path, the Go sink, the bulk loader and the load-to-CDC handoff.

- Norbert Funke: `legacy-kafka-sink/valkey_glide_cluster.py`, and 41% of `legacy-kafka-sink/valkey_default.py` (see "Both").
- Dinesh Kumar: every other file, except those listed under "Both".
- Both: `legacy-kafka-sink/valkey_default.py` and `PROTOTYPE_README.md` (started by Norbert, extended by Dinesh); parts of `data-plane/sink-worker-v3/` derive from Norbert's earlier sink.

Each snapshot commit has the main author of its files (by `git blame` on the prototype) as its git author, except `legacy-kafka-sink/valkey_default.py`: Norbert started it and wrote 41% of its lines, so it is in his commit. The prototype's own commit history is not replayed.

## What changed on import

- **Removed:** compiled binaries, a SQLite database, office documents, the retired web UI and control plane (with its agent builder and ECS task definitions), the legacy EC2 and Kafka deployment path and its CLI, the SQL query proxy and its benchmark (not ported, see #1412), environment-specific operations and troubleshooting scripts, a generated dashboard file, raster diagrams, agent and steering files, and the prototype's own ignore files (the root `.gitignore` keeps `demo/demo.env` out of git).
- **Replaced:** account ids, endpoints, container image URIs, security group, subnet group and bucket names, and region defaults are now required parameters or placeholders such as `<account-id>` and `<region>`. The prototype's working name is replaced by `CDC-cache` (`cdc-cache`, `CDC_CACHE_`), and files named after it were renamed to match.
- **Credentials:** none are committed, and none is a template parameter, default, plain environment variable or file. The templates take only the ARN of a Secrets Manager secret (JSON with `username` and `password`) and inject both into the tasks through ECS `secrets` (`valueFrom`); the scripts read them from that secret at run time. The one exception is the Aurora templates' `MasterUsername`, which RDS requires: it is a required parameter with no default, and RDS generates the password and keeps it in Secrets Manager.
- **Data:** the sample data is synthetic and produced by the generators here (seeders, load generators, demo inserts); no dataset, dump or captured output is included, and environment resource names in the benchmark write-ups are placeholders.
- **Kept as written otherwise:** code, configuration and documents are the authors' own, including the shortcuts the port will fix. The authors' overview is [`PROTOTYPE_README.md`](PROTOTYPE_README.md); references in it to removed parts were dropped.

## Layout

| Path | What |
|---|---|
| `PROTOTYPE_README.md` | The authors' overview, quickstart and architecture summary |
| `quickstart.sh` | The authors' one-command deploy, seed, load and verify script |
| `data-plane/` | Debezium Server configs (MySQL, PostgreSQL), the bulk loader and handoff orchestrator, the "safe to read" gate, the transform spec and its fixtures, and an alternate Python sink |
| `tools/` | The Go sink (canonical writer), Go bulk loader, stream splitter, aggregator, benchmark generators and the live observability panel |
| `legacy-kafka-sink/` | The earlier Python Kafka sinks (aggregations, write modes, last-writer-wins), for reference |
| `infra/` | CloudFormation: Aurora Serverless sources, the Valkey broker and target, Debezium and the sink on ECS Fargate |
| `examples/my-pipeline/setup/` | The sample workload `quickstart.sh` uses |
| `demo/` | The authors' demo scripts and per-stage monitors |
| `docs/` | Architecture and the benchmark write-ups |

## Licence

Covered by the repository's [LICENSE](../../LICENSE) (MIT-0), like the rest of the repository; there are no per-file licence headers.
