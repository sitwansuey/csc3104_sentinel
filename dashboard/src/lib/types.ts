export interface Target { cluster_id: string; namespace: string; kind: string; name: string; uid: string }
export interface Source { connected?: boolean; status?: string; error_code?: string; partial?: boolean; detail?: string }
export interface WireMessage {
  schema_version: string; kind: string; incident_id: string; action_id?: string; target: Target;
  action?: string; parameters?: Record<string, unknown>; based_on_resource_version?: string;
  rationale?: string; evidence_refs?: string[]; signals?: { reason: string; source: string; detail: string; at: string }[];
  verdict?: string; reason_codes?: string[]; policy_version?: string; validated_resource_version?: string;
  checked_at?: string; recorded_at?: string; observed_at?: string; valid_until?: string;
  proposal_sha256?: string; inputs_sha256?: string; status?: string; resource_version?: string | null;
  details?: string; affected_services?: string[]; blockers?: string[]; unknowns?: string[];
  window_start?: string; window_end?: string; checks?: { name: string; passed: boolean; detail: string }[];
}
export interface Incident { sequence: number; incident_id: string; target: Target; state: string; active_action_id: string | null }
export interface Action { action_id: string; incident_id: string; proposal: WireMessage; state: string; guard_result: WireMessage | null; execution_result: WireMessage | null }
export interface Telemetry { sequence: number; at: string; component: number; event_type: string; summary: string; target: Target | null; incident_id?: string | null; action_id?: string | null; metrics: Record<string, number> }
export interface Approval { action_id: string; operator_id: string; decision: string; approved_at: string; expires_at?: string; proposal_sha256?: string; policy_version?: string }
export interface Finding { code: string; status: string; path: string; message: string; actual: unknown; expected: unknown }
export interface GuardReport { action_id: string; checked_at: string; inputs_sha256: string; proposal_sha256: string; checks: Finding[] }
export interface ClusterNode { name: string; ready: boolean | null; conditions: { type: string; status: string }[]; cpu_allocatable_cores: number | null; memory_allocatable_bytes: number | null; cpu_usage_cores: number | null; memory_usage_bytes: number | null; metrics_timestamp?: string | null; metrics_window_seconds?: number | null }
export interface Pod { name: string; namespace: string; node: string | null; phase: string; ready: boolean | null; containers_ready: number; containers_total: number; restarts: number | null; reason: string | null; cpu_usage_cores: number | null; memory_usage_bytes: number | null; metrics_timestamp?: string | null }
export interface Cluster {
  cluster_id: string; namespace: string; observed_at: string; sources: Record<string, Source>;
  nodes: ClusterNode[]; pods: Pod[]; events: { reason: string; object: string; count: number | null; last_seen: string | null }[];
  summary: Record<string, number | null>; truncation: Record<string, number>;
}
export interface Snapshot {
  generated_at: string; mode: string; demo_step?: string; limit: number; sources: Record<string, Source>;
  incidents: Incident[]; actions: Action[]; messages: { sequence: number; message: WireMessage }[];
  approvals: { id: number; record: Approval }[]; outbox: { id: number; topic: string; action_id: string | null; delivered: number }[];
  reports: GuardReport[]; telemetry: Telemetry[]; cluster_history: Cluster[];
}
export type View = "overview" | "cluster" | "coordinator" | "monitoring" | "planner" | "guard" | "executor" | "verification"
