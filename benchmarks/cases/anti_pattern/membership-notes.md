# Anti-pattern case membership notes

Why a committed case's query list changed, case by case, for whoever reviews its sheet. The lists are written by `uv run python -m benchmarks.runner.anti_pattern_cases`, never by hand; these notes only record the evidence for a change. They say which queries joined or left a case, not which rows the pipeline flags: that stays out of the review, as the blind sheets require (ADR-030 Amendment 2).

## 2026-11-06, #1955: the refreshed wordpress sample

The wordpress sample was collected again, after a statistics reset and a seeded workload through the shop's web and REST interfaces (`docs/examples/wordpress/README.md`): 163 statements instead of 107, with the writes the old file lacked, and every statement's window about 27 minutes of that workload. Rates rose for almost every statement and new tables gained traffic, so the builder selected the three wordpress cases again; the selection rule did not change, and all three cases are still unreviewed. Rates below are calls per second, on the old file and then on the new one, both over each statement's own window (`-` for a statement the old file does not hold).

### wordpress-frequent-full-scan

- Left: `52da34b1…` (`SELECT p.ID FROM wp_posts p WHERE post_type = ? AND p.post_title = ? …`): 1.732 → 19.249; `d4bf16d6…` (`SELECT SQL_CALC_FOUND_ROWS wp_posts.ID FROM wp_posts WHERE … post_parent = ? …`): 1.625 → 3.471.
- Joined: `1794daed…` (`SELECT key_id, user_id, permissions, consumer_key, … FROM wp_woocommerce_api_keys …`): 1.351 → 8.497; `413c99e4…` (`SELECT option_name, option_value FROM wp_options WHERE autoload = ?`): 0.867 → 9.627.
- Stayed: `329367a2…` (`SELECT * FROM wp_wc_tax_rate_classes ORDER BY NAME`): 1.649 → 9.147.

### wordpress-no-relational-need

- Left: `2c0093ed…` and `33dfdb12…` (`UPDATE wp_postmeta SET meta_value = meta_value ± ? …`): 0.291 → 0.865 and 0.292 → 0.865; `59163c18…` (`SELECT a.action_id FROM wp_actionscheduler_actions a LEFT JOIN wp_actionscheduler_groups g …`): 0.743 → 3.160; `966f5a33…` (`SELECT t.term_id FROM wp_terms AS t INNER JOIN wp_term_taxonomy AS tt …`): 0.381 → 2.627; `97834e68…` (`SELECT p.ID FROM wp_posts p INNER JOIN wp_term_relationships tr …`): 0.743 → 3.160.
- Joined: `22726c84…` (`SELECT t.term_id FROM wp_terms AS t INNER JOIN wp_term_taxonomy AS tt …`): 0.676 → 3.867; `413c99e4…` (`SELECT option_name, option_value FROM wp_options WHERE autoload = ?`): 0.867 → 9.627; `41b2f2d1…` (`UPDATE wp_usermeta SET meta_value = ? WHERE user_id = ? AND meta_key = ?`): - → 1.274; `4b856e29…` (`UPDATE wp_woocommerce_order_itemmeta SET meta_value = ? …`): 0.317 → 1.464; `67fcc3ec…` (`SELECT umeta_id AS meta_id, meta_key, meta_value FROM wp_usermeta WHERE user_id = ? …`): - → 8.309; `d4bf16d6…` (`SELECT SQL_CALC_FOUND_ROWS wp_posts.ID FROM wp_posts WHERE … post_parent = ? …`): 1.625 → 3.471; `eb232c6c…` (`SELECT umeta_id FROM wp_usermeta WHERE meta_key = ? AND user_id = ?`): - → 10.975; `f37cfe51…` (`SELECT DISTINCTROW t.term_id, tr.object_id FROM wp_terms AS t …`): 0.276 → 1.926.
- Stayed: `34b8682e…` (0.414 → 1.985), `3c5c8a79…` (0.292 → 1.222), `50b46567…` (0.409 → 2.136), `ee386e4a…` (0.875 → 3.768).

### wordpress-simple-cache-read

- Joined: `1da3b858…` (`SELECT a.*, g.slug AS group FROM wp_actionscheduler_actions a LEFT JOIN …`): 2.164 → 23.974; `22cb9563…` (`SELECT meta_id FROM wp_woocommerce_order_itemmeta WHERE meta_key = ? AND order_item_id = ?`): 3.164 → 14.632; `8485f064…` (`SELECT order_item_id, meta_key, meta_value FROM wp_woocommerce_order_itemmeta WHERE order_item_id IN (...) …`): 14.222 → 41.115; `96ca5829…` (`SELECT meta_id AS meta_id, meta_key, meta_value FROM wp_woocommerce_order_itemmeta WHERE order_item_id = ? …`): 10.393 → 24.770; `bdb34acb…` (`SELECT SUM(order_item_meta__qty.meta_value) AS order_item_count FROM wp_posts AS posts …`): 0.058 → 0.293; `f86b4be6…` (`SELECT im.meta_value FROM wp_posts AS p INNER JOIN wp_postmeta AS pm …`): - → 0.122.
- Stayed: `3dbc7d9a…` (23.765 → 92.244), `4f024754…` (17.022 → 89.421), `71b98418…` (7.839 → 37.247).

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
