# Step 1 LOB sizes

The collection did not measure the LOB columns of 16 moved tables (wordpress.wp_actionscheduler_actions, wordpress.wp_commentmeta, wordpress.wp_links, wordpress.wp_options, wordpress.wp_postmeta, wordpress.wp_posts, wordpress.wp_term_taxonomy, wordpress.wp_termmeta, wordpress.wp_usermeta, wordpress.wp_wc_admin_note_actions and 6 more). The MySQL source endpoint truncates a value longer than the task's LOB limit with no task error, so step 1 does not guess a size: preflight's `lob_sizes` check stops until each table is measured.

The modernizer never connects to your databases: you run `lob-sizes.sql` on the source with your own credentials, and `/move` reads only its output file.

- Read-only: the script sets its session read-only and reads each table in its own `START TRANSACTION READ ONLY` transaction. Use a user that may only read these tables.
- What the file holds: per LOB column, the longest value's length in bytes (`MAX(LENGTH(column))`) and the non-NULL count, never a value or a name.
- Cost: each table is read in full (a full scan), one table at a time. Run it when the source can take that load. Each table's READ ONLY transaction keeps one read view open for its whole scan, which holds back InnoDB's undo purge on a large, busy table until it ends.

## Run it

With the MySQL client:

```
mysql --host=<source host> --user=<read-only user> -p --ssl-mode=VERIFY_IDENTITY --default-character-set=utf8mb4 --batch --skip-column-names --force < lob-sizes.sql > <artifact root>/wordpress/099e6873/data-movement/step-1/lob-sizes.tsv
```

With the MariaDB client (it has no `--ssl-mode`; `--ssl-verify-server-cert` is MySQL's `VERIFY_IDENTITY`):

```
mariadb --host=<source host> --user=<read-only user> -p --ssl-ca=<CA bundle> --ssl-verify-server-cert --default-character-set=utf8mb4 --batch --skip-column-names --force < lob-sizes.sql > <artifact root>/wordpress/099e6873/data-movement/step-1/lob-sizes.tsv
```

Then ask `/move step-1` to read it (`run_dms_step.py lob-sizes`): it sizes each table from the longest value (2 bytes per byte of a character LOB, 1.25 times headroom), writes the plan and `dms-step-1.yaml` again, and the preflight runs again. A table whose query fails (missing, or the user may not read it) prints no line, and the file is refused: fix the access and run the script again. Measure again before the full load if the data grew.

## Skipping the measurement

Only if you choose to: set `"lob_sizes": "skip"` in step 1's `inputs.json` and run `run_dms_step.py lob-sizes`. Each unmeasured table then runs at 1024 KB, the replication instance is sized for that (it costs memory per LOB column), and a longer value is truncated with only a warning in the task log: only DMS validation reports it.
