# Anti-pattern case membership notes

Why a committed case's query list changed, case by case, for whoever reviews its sheet. The lists are written by `uv run python -m benchmarks.runner.anti_pattern_cases`, never by hand; these notes only record the evidence for a change. They say which queries joined or left a case, not which rows the pipeline flags: that stays out of the review, as the blind sheets require (ADR-030 Amendment 2).

## 2026-11-05, #1893: call rates over each statement's own window

Every collector used to divide a statement's execution count by 24 hours. Since #1893 it divides it by the window the count covers: on wordpress (MySQL) and trailgear (SQL Server), each statement's own `first_seen` to `last_seen` (discourse, a PostgreSQL collection with no statistics reset time, keeps its 24-hour rates and its cases did not change). The selection rule did not change; the cases were rebuilt with the builder because the rates it reads moved. Rates below are calls per second, over 24 hours before and over the statement's own window after (the sheets' `calls/s` column shows the new ones).

### wordpress-frequent-full-scan

- Left: `413c99e4…` (`SELECT option_name, option_value FROM wp_options WHERE autoload …`): 1.438 → 0.867, a 39.8-hour window.
- Joined: `52da34b1…` (`SELECT p.ID FROM wp_posts p WHERE post_type = ? …`): 2.874 → 1.732, a 39.8-hour window.
- Stayed: `329367a2…` (1.263 → 1.649) and `d4bf16d6…` (1.245 → 1.625), both 18.4-hour windows.

### wordpress-simple-cache-read (new)

`simple-cache-read` (a high-frequency read of small, rarely-changing data) is reported only for a read above a call-rate threshold. Over 24 hours no wordpress read the 0..40 sweep could move reached it, so the sample had no case; over the statements' own windows one does, so the builder now selects a case. Which query that is, and its threshold, are in pull request #1901, not here, so the sheet stays blind. The case's queries:

- `3dbc7d9a…` (`SELECT meta_id FROM wp_postmeta WHERE meta_key = ? AND post_id = ?`): 14.55 → 23.77 (14.7 hours).
- `4f024754…` (`SELECT post_id, meta_key, meta_value FROM wp_postmeta WHERE …`): 13.04 → 17.02 (18.4 hours).
- `71b98418…` (`SELECT meta_id AS meta_id, meta_key, meta_value FROM wp_postmeta …`): 4.80 → 7.84 (14.7 hours).

### trailgear-frequent-full-scan

- Left: `0x0253E46183218663` (`SELECT o.[SalesOrderID], o.[OrderNumber], l.[ProductID] …`): 1.909 → 2.132; `0x9A123DFBF60D59A1` (`SELECT po.[PurchaseOrderID], po.[Status], po.[OrderedDate] …`): 0.039 → 0.048; `0xFDB1D8615B27F96D` (`SELECT [SettingValue] FROM [StoreSetting] WHERE [SettingKey] = …`): 6.181 → 5.683.
- Joined: `0x2919320F917A4A39` (`SELECT [CustomerID], [Email], [FirstName], [LastName], [Phone] FROM [Customer] …`): 4.939 → 5.866; `0x5903995B9B42F531` (`SELECT pol.[ProductID], pol.[Quantity], pol.[UnitCost] …`): 0.029 → 0.032; `0xDD2E562051788685` (`SELECT [CustomerID], [FirstName], [LastName] FROM [Customer] WHERE [Email] …`): 2.103 → 1.875.
- Stayed: `0x57083C831FBABE20` (9.414 → 8.929), `0x3BE9AE87BC4E81FC` (1.639 → 1.796), `0x61AC3BDB0619D600` (0.042 → 0.038), `0xEA2423F36607A6FE` (5.089 → 5.743), `0x88BF8EE1D634B98E` (2.213 → 2.295), `0xBF0498C3B5E8BD52` (0.040 → 0.038). Each window is 19 to 30 hours, around the sample generator's 24-hour target.

### wordpress-no-relational-need

Its queries did not change; its sheet's `calls/s` and `share` columns were regenerated with the new rates.
