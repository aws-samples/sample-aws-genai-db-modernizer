"""Settings routes — system configuration."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

# In-memory settings for Phase 0. Replace with DynamoDB or SSM in Phase 1.
_settings: dict = {
    "aws_configuration": {
        "region": "us-east-1",
        "s3_bucket": "",
        "dynamodb_table": "",
        "iam_role": "",
    },
    "default_analysis_options": {
        "query_log_period_days": 7,
        "sample_size": 1000,
        "target_databases": ["dynamodb", "documentdb", "elasticache", "opensearch", "aurora"],
        "anonymize_pii": True,
        "include_sample_data": True,
    },
    "ui_preferences": {
        "color_theme": "system",
        "auto_refresh_interval_seconds": 30,
        "browser_notifications": True,
        "email_notifications": False,
        "compact_mode": False,
    },
}


@router.get("")
async def get_settings():
    """Get current settings."""
    return _settings


@router.put("")
async def update_settings(settings: dict):
    """Update settings."""
    _settings.update(settings)
    return _settings


@router.post("/test-connection")
async def test_connection():
    """Test AWS connectivity (S3, DynamoDB)."""
    import boto3

    results = {}

    # Test S3
    bucket = _settings["aws_configuration"].get("s3_bucket")
    if bucket:
        try:
            boto3.client("s3").head_bucket(Bucket=bucket)
            results["s3_bucket"] = {"status": "ok", "message": "Bucket accessible"}
        except Exception as e:
            results["s3_bucket"] = {"status": "error", "message": str(e)}
    else:
        results["s3_bucket"] = {"status": "skipped", "message": "No bucket configured"}

    # Test DynamoDB
    table = _settings["aws_configuration"].get("dynamodb_table")
    if table:
        try:
            boto3.client("dynamodb").describe_table(TableName=table)
            results["dynamodb_table"] = {"status": "ok", "message": "Table accessible"}
        except Exception as e:
            results["dynamodb_table"] = {"status": "error", "message": str(e)}
    else:
        results["dynamodb_table"] = {"status": "skipped", "message": "No table configured"}

    return results
