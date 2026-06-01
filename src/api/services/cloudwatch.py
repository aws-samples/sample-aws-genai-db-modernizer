"""CloudWatch Logs service — reads agent execution logs."""

import boto3


class CloudWatchLogsService:
    def __init__(self, log_group: str):
        self.log_group = log_group
        self.client = boto3.client("logs")

    def get_logs(
        self,
        stream_prefix: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 100,
        next_token: str | None = None,
    ) -> dict:
        """Get log events filtered by stream prefix.

        Args:
            stream_prefix: Filter by log stream prefix (e.g., "collector", "analysis").
            start_time: Start time in epoch milliseconds.
            end_time: End time in epoch milliseconds.
            limit: Max events to return.
            next_token: Pagination token from previous call.
        """
        params: dict = {
            "logGroupName": self.log_group,
            "limit": min(limit, 500),
        }
        if stream_prefix:
            params["logStreamNamePrefix"] = stream_prefix
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        if next_token:
            params["nextToken"] = next_token

        try:
            response = self.client.filter_log_events(**params)
        except self.client.exceptions.ResourceNotFoundException:
            return {"logs": [], "next_token": None}

        logs = [
            {
                "timestamp": event.get("timestamp", 0),
                "message": event.get("message", ""),
                "log_stream": event.get("logStreamName", ""),
            }
            for event in response.get("events", [])
        ]

        return {
            "logs": logs,
            "next_token": response.get("nextForwardToken"),
        }
