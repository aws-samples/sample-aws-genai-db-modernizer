/**
 * Mock API Data
 * Static responses for fake API mode during development
 * Based on API specification from workspace/api.md
 */

export const mockApiResponses = {
  // GET /api/v1/health
  'GET:/health': {
    status: 'healthy',
    version: 'a1b2c3d4e5f6'
  },

  // POST /api/v1/assessments
  'POST:/assessments': {
    job_id: '550e8400-e29b-41d4-a716-446655440000',
    status: 'PENDING',
    created_at: '2026-02-23T14:30:00Z',
    estimated_completion_time: '2026-02-23T20:30:00Z',
    execution_arn: 'arn:aws:states:us-east-1:123456789012:execution:modernizer-dev-job-orchestrator:550e8400...'
  },

  // GET /api/v1/assessments
  'GET:/assessments': {
    assessments: [
      {
        job_id: '550e8400-e29b-41d4',
        source_database_type: 'mysql',
        database_name: 'ecommerce_prod',
        status: 'COMPLETED',
        created_at: '2026-02-23T10:00:00Z',
        completed_at: '2026-02-23T14:12:00Z',
        duration_seconds: 15120,
        progress_percent: 100
      },
      {
        job_id: '6ba7b810-9dad-11d1',
        source_database_type: 'postgresql',
        database_name: 'analytics_db',
        status: 'RUNNING',
        created_at: '2026-02-23T13:30:00Z',
        completed_at: null,
        duration_seconds: 1800,
        progress_percent: 45
      },
      {
        job_id: '6ba7b811-9dad-11d1',
        source_database_type: 'mariadb',
        database_name: 'legacy_system',
        status: 'COMPLETED',
        created_at: '2026-02-22T10:00:00Z',
        completed_at: '2026-02-22T13:45:00Z',
        duration_seconds: 13500,
        progress_percent: 100
      },
      {
        job_id: '550e8401-e29b-41d4',
        source_database_type: 'mysql',
        database_name: 'old_mysql_db',
        status: 'FAILED',
        created_at: '2026-02-20T08:00:00Z',
        completed_at: '2026-02-20T10:05:00Z',
        duration_seconds: 7500,
        progress_percent: 35
      },
      {
        job_id: '6ba7b812-9dad-11d1',
        source_database_type: 'postgresql',
        database_name: 'test_db',
        status: 'PENDING',
        created_at: '2026-02-23T14:25:00Z',
        completed_at: null,
        duration_seconds: 0,
        progress_percent: 0
      }
    ],
    total_count: 24,
    limit: 25,
    offset: 0
  },

  // GET /api/v1/assessments/{job_id}
  'GET:/assessments/:id': {
    job_id: '550e8400-e29b-41d4',
    status: 'RUNNING',
    source_database_type: 'mysql',
    database_name: 'ecommerce_prod',
    created_at: '2026-02-23T14:23:15Z',
    execution_arn: 'arn:aws:states:us-east-1:123456789012:execution:modernizer-dev-job-orchestrator:550e8400...',
    progress: {
      percent_complete: 45,
      current_stage: 'analysis',
      current_activity: 'Running analysis agents (5 of 7 completed)',
      estimated_remaining_seconds: 8100,
      stages: [
        { name: 'collector', status: 'completed', duration_seconds: 900 },
        { name: 'referee-triage', status: 'completed', duration_seconds: 120 },
        { name: 'analysis-dynamodb', status: 'completed', duration_seconds: 480 },
        { name: 'analysis-documentdb', status: 'completed', duration_seconds: 360 },
        { name: 'analysis-elasticache', status: 'in-progress', duration_seconds: 480 },
        { name: 'analysis-aurora', status: 'pending', duration_seconds: null },
        { name: 'analysis-opensearch', status: 'pending', duration_seconds: null },
        { name: 'referee-synthesis', status: 'pending', duration_seconds: null },
        { name: 'schema-design-dynamodb', status: 'pending', duration_seconds: null }
      ]
    }
  },

  // DELETE /api/v1/assessments/{job_id}
  'DELETE:/assessments/:id': {
    job_id: '550e8400-e29b-41d4',
    status: 'CANCELLED',
    message: 'Assessment cancelled successfully'
  },

  // GET /api/v1/assessments/{job_id}/agents
  'GET:/assessments/:id/agents': {
    agents: [
      {
        agent_name: 'collector',
        status: 'completed',
        started_at: '2026-02-23T14:23:16Z',
        completed_at: '2026-02-23T14:38:22Z',
        duration_seconds: 906,
        output_size_bytes: 25796608,
        details: 'Collected 24.6 MB of schema and performance data'
      },
      {
        agent_name: 'analysis-elasticache',
        status: 'in-progress',
        started_at: '2026-02-23T14:38:30Z',
        completed_at: null,
        duration_seconds: 480,
        output_size_bytes: null,
        details: 'Evaluating Redis/ElastiCache compatibility patterns'
      },
      {
        agent_name: 'referee-synthesis',
        status: 'pending',
        started_at: null,
        completed_at: null,
        duration_seconds: null,
        output_size_bytes: null,
        details: 'Will generate final recommendations and architecture'
      }
    ]
  },

  // GET /api/v1/assessments/{job_id}/logs
  'GET:/assessments/:id/logs': {
    logs: [
      {
        timestamp: '2026-02-23T14:23:15Z',
        agent: 'collector',
        level: 'INFO',
        message: 'Starting analysis job 550e8400-e29b-41d4'
      },
      {
        timestamp: '2026-02-23T14:23:16Z',
        agent: 'collector',
        level: 'INFO',
        message: 'Connecting to database endpoint...'
      },
      {
        timestamp: '2026-02-23T14:23:18Z',
        agent: 'collector',
        level: 'INFO',
        message: 'Successfully connected to MySQL 8.0.32'
      },
      {
        timestamp: '2026-02-23T14:23:20Z',
        agent: 'collector',
        level: 'INFO',
        message: 'Collecting schema metadata...'
      }
    ],
    next_token: 'eyJ0...'
  },

  // GET /api/v1/assessments/{job_id}/results
  'GET:/assessments/:id/results': {
    job_id: '550e8400-e29b-41d4',
    status: 'COMPLETED',
    executive_summary: {
      architecture_type: 'MULTI_DATABASE',
      tables_analyzed: 1247,
      confidence_score: 87,
      confidence_level: 'HIGH',
      estimated_monthly_savings: 2200,
      savings_percent: 44
    },
    recommended_architecture: {
      databases: [
        { service: 'DynamoDB', table_count: 850, confidence: 92, pattern: 'Key-value and single-table access' },
        { service: 'DocumentDB', table_count: 297, confidence: 88, pattern: 'Document-oriented and JSON workloads' },
        { service: 'ElastiCache', table_count: 100, confidence: 95, pattern: 'High-frequency caching patterns' }
      ]
    },
    tco_analysis: {
      current_monthly_cost: 5000,
      projected_monthly_cost: 2800,
      monthly_savings: 2200,
      savings_percent: 44,
      three_year_savings: 79200,
      payback_period_months: 3.2,
      roi_three_year_percent: 132,
      cost_breakdown: {
        current: { rds_instance: 4200, storage_backups: 500, data_transfer: 300 },
        projected: { dynamodb: 1200, documentdb: 950, elasticache: 650 }
      }
    },
    risk_assessment: {
      overall_risk_level: 'MEDIUM',
      risks: [
        {
          risk: 'Data consistency',
          severity: 'MEDIUM',
          likelihood: 'MEDIUM',
          impact: 'Potential inconsistency across distributed databases',
          mitigation: 'Implement distributed transactions and eventual consistency patterns'
        }
      ]
    },
    triage_summary: {
      selected_agents: ['dynamodb', 'documentdb', 'elasticache'],
      skipped_agents: ['neptune', 'opensearch', 'keyspaces', 'aurora'],
      triage_confidence: 0.87
    }
  },

  // GET /api/v1/assessments/{job_id}/results/table-mappings
  'GET:/assessments/:id/results/table-mappings': {
    table_mappings: [
      {
        source_table: 'users',
        recommended_db: 'DynamoDB',
        confidence: 95,
        confidence_level: 'HIGH',
        access_pattern: 'Key-value access',
        alternatives: [
          { service: 'DocumentDB', confidence: 82 },
          { service: 'Aurora', confidence: 78 }
        ]
      },
      {
        source_table: 'orders',
        recommended_db: 'DynamoDB',
        confidence: 92,
        confidence_level: 'HIGH',
        access_pattern: 'Single-table design',
        alternatives: [
          { service: 'Aurora', confidence: 85 }
        ]
      },
      {
        source_table: 'products',
        recommended_db: 'DocumentDB',
        confidence: 88,
        confidence_level: 'HIGH',
        access_pattern: 'Document-oriented queries',
        alternatives: [
          { service: 'DynamoDB', confidence: 75 }
        ]
      }
    ],
    total_count: 1247,
    limit: 25,
    offset: 0
  },

  // GET /api/v1/settings
  'GET:/settings': {
    aws_configuration: {
      region: 'us-east-1',
      s3_bucket: 'database-modernizer-results-us-east-1',
      dynamodb_table: 'database-modernizer-jobs',
      iam_role: ''
    },
    default_analysis_options: {
      query_log_period_days: 7,
      sample_size: 1000,
      target_databases: ['dynamodb', 'documentdb', 'elasticache', 'opensearch', 'aurora'],
      anonymize_pii: true,
      include_sample_data: true
    },
    ui_preferences: {
      color_theme: 'system',
      auto_refresh_interval_seconds: 30,
      browser_notifications: true,
      email_notifications: false,
      notification_events: {
        completed: true,
        failed: true,
        warnings: false,
        long_running: false
      },
      compact_mode: false
    }
  },

  // PUT /api/v1/settings
  'PUT:/settings': {
    message: 'Settings updated successfully'
  },

  // POST /api/v1/settings/test-connection
  'POST:/settings/test-connection': {
    s3_bucket: { status: 'ok', message: 'Bucket accessible' },
    dynamodb_table: { status: 'ok', message: 'Table accessible' },
    iam_role: { status: 'skipped', message: 'No IAM role configured' }
  },

  // GET /api/v1/dashboard/stats
  'GET:/dashboard/stats': {
    total_assessments: 24,
    active_jobs: 2,
    success_rate_percent: 95,
    average_duration_hours: 4.2,
    completed_today: 5,
    last_analysis_at: '2026-02-23T12:15:00Z'
  }
};

/**
 * Get mock response for a given method and path
 * @param {string} method - HTTP method (GET, POST, PUT, DELETE)
 * @param {string} path - API path (e.g., '/assessments' or '/assessments/123')
 * @returns {object} Mock response data
 */
export function getMockResponse(method, path) {
  // Normalize path - remove leading slash and trailing slash
  const normalizedPath = path.replace(/^\//, '').replace(/\/$/, '');

  // Try exact match first
  const exactKey = `${method}:/${normalizedPath}`;
  if (mockApiResponses[exactKey]) {
    return mockApiResponses[exactKey];
  }

  // Try pattern match for paths with IDs (e.g., /assessments/123 -> /assessments/:id)
  const pathParts = normalizedPath.split('/');
  if (pathParts.length > 1) {
    // Replace potential ID segments with :id
    const patternPath = pathParts.map((part, index) => {
      // If it looks like a UUID or ID (contains hyphens or is all alphanumeric)
      if (index > 0 && (part.includes('-') || /^[a-zA-Z0-9]+$/.test(part))) {
        return ':id';
      }
      return part;
    }).join('/');

    const patternKey = `${method}:/${patternPath}`;
    if (mockApiResponses[patternKey]) {
      return mockApiResponses[patternKey];
    }
  }

  // Return error if no match found
  return {
    error: 'Mock endpoint not found',
    message: `No mock data defined for ${method}:/${normalizedPath}`
  };
}
