# FastAPI REST API

This directory contains the FastAPI REST API server.

## Files

- **main.py** - FastAPI application; `routes/` holds the routers, `services/` the local execution and file services

## Endpoints

- `GET /health` - Health check
- `/api/v1/assessments/*` - The assessment lifecycle, uploads, per-phase artifacts, the assignment review and resume, schema revisions, deliverables, query journeys and the context graph (routers in `routes/`)
- `GET /api/v1/dashboard/stats` - Dashboard statistics

Progress is polled (`GET /api/v1/assessments/{job_id}`, `/phases`, `/execution-history`); there is no WebSocket. The running server lists every route at `/docs`, and [the high-level design, section 5](../../docs/architecture/high-level-design.md#5-api-architecture) summarises them.

## Running

```bash
uvicorn src.api.main:app --reload --port 8000
```

## Credentials are never sent (ADR-004 Decision 5)

A request names the database credentials by reference only: `connection.secret_arn` (a Secrets Manager ARN) or `connection.secret_name` (a secret name: letters, digits and `/_+=.@!-`, so RDS-managed `rds!db-...` and `rds!cluster-...` secrets work). The other forwarded fields are allowlisted too: `connection.host` is a host name or IP address (optionally `host:port`), `connection.database` a plain name, `connection.credentials_type` is `secrets-arn` or `secrets-name`, the engine, source and mode fields are plain names, and table patterns are table names and wildcards separated by commas (quote a name with spaces: `"Order Details"`, `[Order Details]` or `` `Order Details` ``). A request that carries a password, secret, token or a credential-shaped string anywhere is refused with a 400 and nothing is stored (`secret_guard.py`). Agent answers are free text, so there only a value with a credential shape is refused: an assignment of a literal (`password=...`), a quoted `PASSWORD '...'` or `IDENTIFIED BY ...`, a client flag with a value (`mysql -p<pw>`, `--password <pw>`), or user info in a connection string with a scheme, a bare host or a port. `users.password: bcrypt hash` or `token: rotates daily` is accepted. #1001 adds two forms, refused only when the value is a plain literal (the check is shared, so they also apply to the assessment routes, whose fields are allowlisted anyway) (after one pair of matching outer quotes is removed, none of `` $ { } % ` < > ( ) [ ] ! # `` and no quote, not starting with `@` or `:`, and not `?`, `...`, `***`, `xxxx`, `none`, `null`, `n/a`, `-`, a masked value (`••••••`, `…`, `REDACTED`, `**redacted**`) or a documentation word, in any case: `password`, `passwd`, `pass`, `pw`, `pwd`, `secret`, `token`, `changeme`, `change_me`, `replace_me`, `your-password`, `your_password`, `<letters, _ and ->_here` or `-here` (`YOUR_TOKEN_HERE`, `api-key-here`), `auth_string`, `new_password`, `mypassword`, `example`): a quoted literal in a `SET PASSWORD [FOR <user>] = '<pw>'` statement (or `= PASSWORD('<pw>')`), and `curl -u user:<pw>` inside one curl command (it ends at a newline, `;`, `&&`, `||`, `|`, `&`, a trailing `,` or `.`, or a word that is no flag, flag value or URL). A variable or template there (`"$user:$pass"`, `{{ .Values.pw }}`, `$(cat ~/.token)`) is not refused, and neither is a real password that contains one of those characters. Forms that may state a credential but may as well describe one are saved unchanged, with a warning in the response's `warnings` (`answers look like they contain a credential ...; use a secrets manager reference`), and the stored-file redaction never removes them: prose (`the password is <pw>`), user info before a dotted host (`admin:<pw>@db.example.com`, with or without a path), and an unquoted plain value in `SET PASSWORD FOR app = <pw>` or `CREATE|ALTER USER app PASSWORD <pw>`. Request bodies over 1 MiB get a 413 (`MODERNIZER_MAX_BODY_BYTES` overrides the limit).

Versions before #723 could store a submitted password in `artifacts/<db>/<job>/_meta.json`. When the API starts, a background thread removes any such stored credential (and any credential-shaped agent answer) once, and records `artifacts/.meta/_stored-input-redaction-v2.json` so later starts skip it (v2 since #1001 added the plain-language forms, so a v1 marker from an earlier run does not skip them). A file it cannot read or write is logged by path and skipped, the API starts regardless, and the marker is only recorded when nothing was skipped. Only credentials are removed: long values, deeply nested values and text that merely resembles a credential are kept as they are. To run it by hand, whatever the marker says:

```bash
uv run python scripts/redact_stored_inputs.py --artifact-dir ./artifacts
```

It prints the rewritten and skipped file paths (never their contents) and is safe to run more than once.

Live collection reads the secret named by either form. The API forwards `connection.secret_arn` or `connection.secret_name` as the collector's one secret id (`secret_arn` in the execution input, `SECRET_ARN` for the collector), and the collector passes it to Secrets Manager as `SecretId`, which takes an ARN or a name. When both are given, `credentials_type` picks one (`secrets-arn` or `secrets-name`); when only one is given, that one is used. A name is looked up in the collector's own account and region; a secret shared from another account needs its full ARN.
