"""Kt: Tool and data capability knowledge in RCAFlow."""
from typing import Dict, Any
from .schema import ToolMeta

TOOLS_KNOWLEDGE: Dict[str, ToolMeta] = {
    "profile": ToolMeta(
        name="profile",
        description="Inspect telemetry CSV structure, column names, cardinality, and sample rows before choosing columns.",
        args={
            "file": "Relative CSV path within the telemetry directory."
        },
        returns=[
            "columns: list of column header names",
            "sample: list of first 3 rows as dictionaries",
            "distinct: dictionary of unique value counts per column",
            "rows_scanned: number of rows inspected"
        ],
        preconditions=[
            "Target file must exist in telemetry inventory",
            "Inspect before executing metric, log, or trace queries on unfamiliar tables"
        ]
    ),
    "metrics": ToolMeta(
        name="metrics",
        description="Analyze metric time-series against preceding 60-min baseline to identify anomalies and persistent episodes.",
        args={
            "file": "Relative CSV path",
            "time_column": "Timestamp column name (default 'timestamp')",
            "component_column": "Service or host ID column name (default 'cmdb_id')",
            "kpi_column": "Metric name column (omit if table is in wide format)",
            "value_column": "Numeric value column name",
            "start": "Optional ISO start time in UTC+8",
            "end": "Optional ISO end time in UTC+8"
        },
        returns=[
            "findings: sorted list of anomalous component-kpi pairs with peak scores and onset times",
            "episodes: persistent anomaly intervals (onset, end, duration, sustained status)",
            "baseline_mean: mean value during normal operation baseline",
            "baseline_source: whether preceding 60-min or full series was used"
        ],
        preconditions=[
            "Baseline must be calculated before window filtering",
            "Start and end must be aligned with the incident window"
        ]
    ),
    "logs": ToolMeta(
        name="logs",
        description="Analyze system error messages and error burst onsets, filtering out routine JVM garbage collection.",
        args={
            "file": "Relative CSV path",
            "time_column": "Timestamp column name (default 'timestamp')",
            "component_column": "Service or host ID column name (default 'cmdb_id')",
            "message_column": "Text log message column name (default 'message')",
            "start": "Optional ISO start time in UTC+8",
            "end": "Optional ISO end time in UTC+8"
        },
        returns=[
            "error_count: total matching error log entries",
            "components: error counts grouped by component",
            "examples: representative sample error messages",
            "events: minute-level error burst onsets evaluated against preceding baseline"
        ],
        preconditions=[
            "Routine GC events like 'GC (Allocation Failure)' must be ignored unless accompanied by OutOfMemory",
            "Onset time requires a step-increase above historical baseline rate"
        ]
    ),
    "traces": ToolMeta(
        name="traces",
        description="Analyze distributed trace span duration and transition times to assess latency degradation.",
        args={
            "file": "Relative CSV path",
            "time_column": "Timestamp column name (default 'timestamp')",
            "component_column": "Service or host ID column name (default 'cmdb_id')",
            "duration_column": "Span duration / elapsed time column (default 'duration')",
            "start": "Optional ISO start time in UTC+8",
            "end": "Optional ISO end time in UTC+8"
        },
        returns=[
            "latency: 95th percentile and maximum span latency per component",
            "events: step-function latency transition onsets (3-sigma over rolling median)",
            "note: reminder that latency alone does not prove parent-child causality"
        ],
        preconditions=[
            "Duration must be numeric",
            "Trace latency must be correlated with metrics or logs to establish causality"
        ]
    )
}


def get_tools_knowledge() -> Dict[str, Dict[str, Any]]:
    return {k: v.to_dict() for k, v in TOOLS_KNOWLEDGE.items()}

