# FastAPI REST API

This directory contains the FastAPI REST API server.

## Files

- **main.py** - FastAPI application with endpoints

## Endpoints

- `GET /health` - Health check
- `POST /api/v1/analyses` - Create analysis job
- `GET /api/v1/analyses/{job_id}` - Get analysis status
- `GET /api/v1/analyses/{job_id}/results` - Get analysis results
- `WS /ws/analyses/{job_id}` - WebSocket for real-time updates

## Running

```bash
uvicorn src.api.main:app --reload --port 8000
```
