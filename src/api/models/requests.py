"""Request models for the Database Modernizer Assessment API."""

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError

from src.shared.database_name import validate_database_name as _validate_database_name
from src.shared.job_id import validate_job_id as _validate_job_id
from src.shared.target_databases import normalize_target_databases

_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_DATABASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_$.\-]{0,127}")
HOST_ERROR = (
    "host must be a host name, an IP address or host:port (letters, digits, '-' and '.' "
    "only: no '_', '@', '/', '=', spaces or a trailing dot)."
)
_SECRET_ARN = re.compile(
    r"arn:aws[a-z-]{0,20}:secretsmanager:[a-z0-9-]{1,30}:[0-9]{1,12}:secret:[A-Za-z0-9/_+=.@!-]{1,512}"
)
ARN_REFERENCE_ERROR = (
    "secret_arn must be an AWS Secrets Manager secret ARN (arn:aws:secretsmanager:...)."
)

_LETTERS_DIGITS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
#: The characters AWS Secrets Manager allows in a secret name (no spaces), plus the "!"
#: of the secrets RDS manages itself (rds!db-..., rds!cluster-...).
_SECRET_NAME_CHARS = _LETTERS_DIGITS | frozenset("/_+=.@!-")
#: Table names and LIKE/glob wildcards, separated by commas (spaces around commas are
#: dropped first). A name with spaces must be quoted: "Order Details", [Order Details] or
#: `Order Details`.
_TABLE_PATTERN_CHARS = _LETTERS_DIGITS | frozenset("_%*.$-")
#: Inside a quoted name: words, spaces and wildcards, but no word starting with "-" (so
#: no client flag) and none of = : / @ ' (so no assignment, user info or literal).
_QUOTED_NAME_CHARS = _LETTERS_DIGITS | frozenset("_%*$- ")
_QUOTE_CLOSERS = {'"': '"', "[": "]", "`": "`"}
TABLE_PATTERN_ERROR = (
    "table patterns must be table names or wildcards (letters, digits, _ % * . $ -), "
    'separated by commas; quote a name with spaces as "Order Details", [Order Details] or '
    "`Order Details`."
)


def _valid_table_pattern(element: str) -> bool:
    """One comma-separated element: bare characters and quoted names (a linear scan)."""
    if not element:
        return False
    i, n = 0, len(element)
    while i < n:
        c = element[i]
        if c in _QUOTE_CLOSERS:
            j = element.find(_QUOTE_CLOSERS[c], i + 1)
            inner = element[i + 1 : j] if j != -1 else ""
            if not inner.strip() or not set(inner) <= _QUOTED_NAME_CHARS:
                return False
            if any(word.startswith("-") for word in inner.split()):
                return False
            i = j + 1
        elif c in _TABLE_PATTERN_CHARS:
            i += 1
        else:
            return False
    return True


#: Engine, source and mode names: lower-case words joined by '-' or '_'.
_NAME_CHARS = _LETTERS_DIGITS | frozenset("_-")
_S3_KEY_CHARS = _LETTERS_DIGITS | frozenset("_-./")


def _only(value: str, allowed: frozenset[str], max_len: int, error: str) -> str:
    """``value`` if it is 1 to ``max_len`` characters, all in ``allowed`` (a set check:
    linear, no regex)."""
    if not value or len(value) > max_len or not set(value) <= allowed:
        raise ValueError(error)
    return value


def validate_name(value: str, field_name: str) -> str:
    return _only(
        value.strip(),
        _NAME_CHARS,
        64,
        f"{field_name} must be 1 to 64 letters, digits, '-' or '_'.",
    )


DATABASE_ERROR = (
    "database must be 1 to 128 letters, digits or '_', '$', '.', '-' (no '@', '/', '=', ':' "
    "or spaces)."
)


def _valid_port(text: str) -> bool:
    return text.isdigit() and len(text) <= 5 and 1 <= int(text) <= 65535


def _valid_host_name(name: str) -> bool:
    if not name or len(name) > 253:
        return False
    return all(_HOST_LABEL.fullmatch(label) for label in name.split("."))


def validate_host(value: str) -> str:
    """A host name, IPv4/IPv6 address, or either with ``:port``; nothing else.

    An allowlist rather than a credential check: ``user:pw@host``, ``scott/pw@db``,
    ``jdbc:...`` or ``host PGPASSWORD=...`` can never reach the execution input (#723).
    """
    host = value.strip()
    if not host or len(host) > 260:
        raise ValueError(HOST_ERROR)
    if host.startswith("["):  # [v6] or [v6]:port
        inner, bracket, rest = host[1:].partition("]")
        if bracket and (not rest or (rest[:1] == ":" and _valid_port(rest[1:]))):
            try:
                ipaddress.IPv6Address(inner)
                return host
            except ValueError:
                pass
        raise ValueError(HOST_ERROR)
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    name, colon, port = host.partition(":")
    if colon and not _valid_port(port):
        raise ValueError(HOST_ERROR)
    if not _valid_host_name(name):
        raise ValueError(HOST_ERROR)
    return host


class ConnectionDetails(BaseModel):
    """Where the source database is and which secret holds its credentials.

    Credentials are only ever named by reference -- a secrets-manager ARN or name -- and
    never sent (ADR-004 Decision 5, #723). There is no username or password field, and the
    fields that are forwarded are allowlisted: ``host`` is a host name or IP (optionally
    with ``:port``), ``database`` a plain name, ``credentials_type`` one of two values. A
    request that carries a secret anywhere else is refused by ``src.api.secret_guard``
    before it gets here.
    """

    host: str
    port: int = Field(ge=1, le=65535)
    database: str
    credentials_type: Literal["secrets-arn", "secrets-name"] = "secrets-arn"
    secret_arn: str | None = None
    secret_name: str | None = None

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return validate_host(value)

    @field_validator("database")
    @classmethod
    def _check_database(cls, value: str) -> str:
        stripped = value.strip()
        if not _DATABASE.fullmatch(stripped):
            raise ValueError(DATABASE_ERROR)
        return stripped

    @field_validator("secret_arn")
    @classmethod
    def _check_secret_arn(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        if not _SECRET_ARN.fullmatch(value.strip()):
            raise ValueError(ARN_REFERENCE_ERROR)
        return value.strip()

    @field_validator("secret_name")
    @classmethod
    def _check_secret_name(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        return _only(
            value.strip(),
            _SECRET_NAME_CHARS,
            512,
            "secret_name must be an AWS Secrets Manager secret name: letters, digits and "
            "/_+=.@!- (no spaces). To pass a full ARN, use secret_arn.",
        )

    @field_validator("credentials_type", mode="before")
    @classmethod
    def _normalize_credentials_type(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    def secret_reference(self) -> str:
        """The secret id live collection reads: the ARN or the name, whichever was given.

        The collector passes it to Secrets Manager as ``SecretId``, which takes either form
        (#991). ``credentials_type`` picks one when both are set; otherwise the one given is
        used, and ``""`` when neither is.
        """
        by_arn, by_name = self.secret_arn or "", self.secret_name or ""
        if self.credentials_type == "secrets-name":
            return by_name or by_arn
        return by_arn or by_name


class CollectionOptions(BaseModel):
    anonymize_pii: bool = True
    include_sample_data: bool = True
    sample_size: int = Field(default=1000, ge=100, le=10000)
    query_log_period_days: int = Field(default=7, ge=1, le=30)
    query_log_source: str = "performance-insights"
    include_table_patterns: str | None = None
    exclude_table_patterns: str | None = None
    top_sql_queries: int = 100
    top_wait_events: int = 50

    @field_validator("query_log_source")
    @classmethod
    def _check_query_log_source(cls, value: str) -> str:
        return validate_name(value, "query_log_source")

    @field_validator("include_table_patterns", "exclude_table_patterns")
    @classmethod
    def _check_table_patterns(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        parts = [part.strip() for part in value.split(",")]
        if len(value) > 4096 or not all(_valid_table_pattern(part) for part in parts):
            raise ValueError(TABLE_PATTERN_ERROR)
        return ",".join(parts)


# ``source_database_type`` for an offline upload whose engine the collector reads from
# the file's own metadata (#1177); the UI's default.
DETECT_FROM_FILE = "auto"


def _field_error(field: str, message: str) -> ValidationError:
    """A validation error whose ``loc`` names ``field`` (#1303).

    A model_validator's plain ValueError has an empty field path, so the 422 said ``["body"]``
    and a client could not tell which input to fix. The text is the same as the ValueError's.
    """
    return ValidationError.from_exception_data(
        "AssessmentRequest",
        [
            InitErrorDetails(
                type=PydanticCustomError(
                    "value_error", "Value error, {reason}", {"reason": message}
                ),
                loc=(field,),
                input=None,
            )
        ],
    )


class AssessmentRequest(BaseModel):
    source_database_type: str
    database_name: str
    connection: ConnectionDetails | None = None
    options: CollectionOptions | None = None
    target_databases: list[str] | None = Field(
        None,
        description=(
            "The engine scope (#1328): the active engine catalog ids the job may route "
            'to, for example ["dynamodb", "aurora_postgresql"]. Left out means every '
            "engine; an empty list, or a legacy, planned or unknown id, is a 422"
        ),
    )
    engines_in_use: list[str] | None = Field(
        None,
        description=(
            "The other AWS databases the application runs today (#1071): active engine "
            'ids, for example ["opensearch", "elasticache"]. The collector merges them '
            "with the collection's own list; left out declares nothing"
        ),
    )
    full_analysis: bool = False
    collection_mode: Literal["live", "ddl", "offline"] = "live"
    offline_s3_key: str | None = None
    cluster_id: str | None = (
        None  # RDS instance/cluster ID — auto-discovers VPC, SG, port for live mode
    )
    job_id: str | None = None  # Pre-created job ID from /assessments/prepare

    @field_validator("database_name")
    @classmethod
    def _check_database_name(cls, value: str) -> str:
        return _validate_database_name(value)

    @field_validator("collection_mode", mode="before")
    @classmethod
    def _normalize_collection_mode(cls, value: object) -> object:
        # The collector acts only on the exact lower-case names, so "LIVE" must not skip
        # the live checks (#1290); anything else is a 422 from the Literal.
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("source_database_type")
    @classmethod
    def _check_names(cls, value: str, info) -> str:  # type: ignore[no-untyped-def]
        return validate_name(value, info.field_name)

    @field_validator("target_databases")
    @classmethod
    def _check_target_databases(cls, value: list[str] | None) -> list[str] | None:
        """The engine scope (#1328): active catalog ids, lower-cased, each once, sorted.

        Left out (``None``) is no scope, every engine. The character allowlist runs
        first, so a refused value is never echoed unless it is a plain name.
        """
        if value is None:
            return value
        return normalize_target_databases([validate_name(v, "target_databases") for v in value])

    @field_validator("engines_in_use")
    @classmethod
    def _check_engines_in_use(cls, value: list[str] | None) -> list[str] | None:
        """Engine ids the source runs today, as ``--engines-in-use`` reads them (#1071)."""
        if value is None:
            return value
        from src.shared.engines_in_use import parse_engines_in_use_option

        ids = [validate_name(v, "engines_in_use") for v in value]
        try:
            return parse_engines_in_use_option(",".join(ids))
        except ValueError as exc:
            raise ValueError(str(exc).replace("--engines-in-use", "engines_in_use")) from exc

    @field_validator("cluster_id")
    @classmethod
    def _check_cluster_id(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        return validate_name(value, "cluster_id")

    @field_validator("offline_s3_key")
    @classmethod
    def _check_offline_s3_key(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        return _only(
            value,
            _S3_KEY_CHARS,
            1024,
            "offline_s3_key must be an artifact key: letters, digits and _ - . / only.",
        )

    @field_validator("job_id")
    @classmethod
    def _check_job_id(cls, value: str | None) -> str | None:
        # job_id is optional: omitting it lets the server generate one
        # (always valid), so None passes through unchecked.
        if value is None:
            return value
        return _validate_job_id(value)

    @model_validator(mode="after")
    def validate_connection_required(self):
        if self.collection_mode in ("live", "ddl") and not self.connection:
            raise _field_error("connection", "connection is required for live and ddl modes")
        # Live collection reads the database credentials from one secret, so a live
        # request must name it; otherwise it only fails later, in the collector (#1024).
        if (
            self.collection_mode == "live"
            and self.connection
            and not self.connection.secret_reference()
        ):
            raise _field_error(
                "connection",
                "live mode needs a secret reference: set connection.secret_arn or "
                "connection.secret_name",
            )
        if self.collection_mode == "offline" and not self.offline_s3_key:
            raise _field_error("offline_s3_key", "offline_s3_key is required for offline mode")
        if self.source_database_type == DETECT_FROM_FILE and self.collection_mode != "offline":
            raise _field_error(
                "collection_mode",
                f"source_database_type {DETECT_FROM_FILE!r} (detect from the file) needs "
                "collection_mode offline: a live or DDL collection has no file to read it from",
            )
        return self


class PrepareAssessmentRequest(BaseModel):
    database_name: str
    source_database_type: str = "mysql"

    @field_validator("database_name")
    @classmethod
    def _check_database_name(cls, value: str) -> str:
        return _validate_database_name(value)

    @field_validator("source_database_type")
    @classmethod
    def _check_source_database_type(cls, value: str) -> str:
        return validate_name(value, "source_database_type")
