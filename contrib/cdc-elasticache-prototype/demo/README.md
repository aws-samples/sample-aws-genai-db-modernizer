# CDC-cache Demo Scripts

Run these on an in-VPC host (the EC2 or VPC-attached CloudShell) with the repo
present. They prove the pipeline works end to end and show per-stage observability.

Prereqs: `python3`, `pip3 install pymysql valkey`, and the `cdc-cache-demo-*`
CloudFormation stacks deployed (see repo `quickstart.sh deploy` + `cdc`).

## Setup (once per shell)
```bash
cd demo
source ./00_env.sh          # resolves SRC / TGT / BRK / PW automatically
```
If it prints `PW = (MISSING)` this host can't read CloudFormation/Secrets — copy
`demo.env.example` to `demo.env`, fill in the endpoints + the DB secret ARN, and re-source.

## The demo, in order

### 1. Initial load works
```bash
./01_initial_load.sh
```
Seeds 5 active + 2 inactive products, bulk-loads with transforms, and shows the
active rows cached with a computed `price_cents` while inactive rows are filtered
out. This is the one-time hydration path (parallel bulk loader).

### 2. Live CDC works
```bash
./02_cdc_live.sh
```
Inserts a product in Aurora and shows it reaching Valkey in milliseconds with the
transform applied, then shows an inactive insert being filtered. This is the
steady-state path: source change -> Debezium -> broker -> sink -> target.

### 3. Observability (per stage)
```bash
./03_observe.sh          # trace ONE record through all 4 stages with timing
./03_observe.sh panel    # live panel; run 02 (or inserts) in another terminal
```
Shows: Debezium->broker (`entries-added`), sink consume (`entries-read`, `lag`),
sink->target (`CurrItems` / keys). `lag = entries-added - entries-read`; 0 = caught up.

## CloudWatch (from the console or a shell with read access)
- Metrics: ElastiCache (`cdc-cache-demo-valkey-*`, `cdc-cache-demo-broker-cache-*`),
  ECS (`cdc-cache-demo-cdc-debezium`, `cdc-cache-demo-cdc-sink`).
- Logs: `aws logs tail /ecs/cdc-cache-demo-cdc/sink --follow` and `.../debezium`.

## Talk track
- "Same transform spec runs on both the initial load and live CDC, so the cache
  is identical either way - filter inactive, compute price_cents."
- "A change is readable in the cache in milliseconds; the database stays the
  source of truth, the cache is a read tier that scales independently."
- "Every stage is observable, and `lag` is the single number that says whether
  the cache is caught up."
