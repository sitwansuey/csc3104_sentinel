import type { ReactNode } from "react"
import { Activity, ArrowRight, CheckCheck, GitBranch, Search, ShieldCheck, Sparkles, Terminal } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Empty, GuardDecisionStatus, Panel, Status } from "@/components/records"
import { ReadinessChart } from "@/components/metrics"
import { identity, isNumber, targetLabel, time, type Scoped } from "@/lib/data"
import type { ChartRange } from "@/lib/chart-data"
import type { Snapshot, View, WireMessage } from "@/lib/types"

interface Props {
  data: Scoped
  snapshot: Snapshot
  range: ChartRange
  now: number
  transportStale: boolean
  navigate: (view: View) => void
  inspect: (id: string) => void
}

const settled = new Set(["RECOVERED", "REJECTED"])
const stamp = (m?: WireMessage) => m?.checked_at || m?.recorded_at || m?.window_end || m?.observed_at
const names: Record<string, string> = {
  incident: "Failure detected", proposal: "Repair proposed", dependency_result: "Dependencies assessed",
  guard_result: "Guard evaluated", execution_result: "Execution recorded", outcome: "Recovery evaluated",
}
const checkNames: Record<string, string> = {
  execution: "Execution evidence", sample_count: "Enough health samples", sample_identity: "Correct service identity",
  sample_timestamps: "Valid observation times", stable_window: "Sustained health window", sample_gaps: "Continuous evidence",
  service_health: "Application response and replicas", dependency_health: "Declared dependency health",
  sample_configuration: "Approved template preserved", evidence_collection: "Evidence collection",
}

export function RecoveryOverview({ data, snapshot, range, now, transportStale, navigate, inspect }: Props) {
  const demo = snapshot.mode === "synthetic"
  const incidents = [...data.incidents].sort((a, b) => b.sequence - a.sequence)
  const incident = incidents.find(i => !settled.has(i.state)) || incidents[0]
  const action = data.actions.find(a => a.action_id === incident?.active_action_id &&
    a.incident_id === incident.incident_id && identity(a.proposal.target) === identity(incident.target))
  // Scope every milestone to one incident, exact target and active action. A previous
  // successful action must never make a newer incident appear repaired.
  const messages = data.messages.filter(({ message: m }) => incident &&
    m.incident_id === incident.incident_id && identity(m.target) === identity(incident.target) &&
    (m.kind === "incident" || Boolean(action && m.action_id === action.action_id)))
    .sort((a, b) => a.sequence - b.sequence)
  const latest = (kind: string) => messages.filter(r => r.message.kind === kind).at(-1)?.message
  const detection = latest("incident")
  const proposal = latest("proposal") || action?.proposal
  const bound = (m?: WireMessage | null) => m && action && m.action_id === action.action_id &&
    m.incident_id === incident?.incident_id && identity(m.target) === identity(incident?.target) ? m : undefined
  const guard = latest("guard_result") || bound(action?.guard_result)
  const execution = latest("execution_result") || bound(action?.execution_result)
  const dependencies = latest("dependency_result")
  const outcome = latest("outcome")
  const observations = data.telemetry.filter(e => e.event_type === "observation" && e.target)
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
  const target = incident?.target || observations[0]?.target
  const workloadObservations = observations.filter(e => identity(e.target) === identity(target))
  const observation = workloadObservations[0]
  const elapsed = observation ? now - Date.parse(observation.at) : NaN
  const recent = demo || (!transportStale && Number.isFinite(elapsed) && elapsed >= 0 && elapsed <= 15000)
  const replicasKnown = isNumber(observation?.metrics.ready) && isNumber(observation?.metrics.desired)
  const ready = replicasKnown ? `${observation.metrics.ready} / ${observation.metrics.desired}` : "Unknown"
  const readiness = !observation ? "No observations" : !recent ? "Last known" : !replicasKnown ? "Unknown" :
    observation.metrics.desired > 0 && observation.metrics.ready >= observation.metrics.desired ? "Ready" : "NotReady"
  const chart = [...workloadObservations].reverse().map(e => ({
    time: time(e.at), capturedAt: e.at, ready: e.metrics.ready, desired: e.metrics.desired,
  }))
  const previousDetection = Math.max(0, ...data.messages.filter(r => r.message.kind === "incident" &&
    r.message.incident_id !== incident?.incident_id && identity(r.message.target) === identity(target) &&
    Date.parse(r.message.observed_at || "") < Date.parse(detection?.observed_at || ""))
    .map(r => Date.parse(r.message.observed_at || "")).filter(Number.isFinite))
  const fault = data.telemetry.filter(e => e.event_type === "fault" && identity(e.target) === identity(target) &&
    detection?.observed_at && Date.parse(e.at) <= Date.parse(detection.observed_at) && Date.parse(e.at) > previousDetection)
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))[0]
  const harnessProposal = /operator-authored|harness/i.test(proposal?.rationale || "")
  const stages: { label: string; role: string; view: View; icon: typeof Activity; value: ReactNode; detail: string }[] = [
    { label: "Detect", role: "Role 2", view: "monitoring", icon: Activity,
      value: <Status value={detection ? "RECORDED" : "WAITING"}/>, detail: "Observer evidence" },
    { label: "Propose", role: "Role 3", view: "planner", icon: Sparkles,
      value: <Status value={proposal ? "RECORDED" : "WAITING"}/>, detail: harnessProposal ? "Harness proposal" : "Submitted proposal" },
    { label: "Dependencies", role: "Role 6", view: "verification", icon: GitBranch,
      value: <Status value={dependencies?.status || "WAITING"}/>, detail: "Reviewed service graph" },
    { label: "Authorize", role: "Role 4", view: "guard", icon: ShieldCheck,
      value: guard ? <GuardDecisionStatus decision={guard} demo={demo} transportStale={transportStale} now={now}/> : <Status value="WAITING"/>, detail: "Policy and approval" },
    { label: "Execute", role: "Role 5", view: "executor", icon: Terminal,
      value: <Status value={execution?.status || "WAITING"}/>, detail: "Restricted repair" },
    { label: "Verify", role: "Role 6", view: "verification", icon: CheckCheck,
      value: <Status value={outcome?.status || "WAITING"}/>, detail: "Measured recovery outcome" },
  ]
  const checks = outcome?.checks || []
  const signals = detection?.signals || []
  const unresolved = incidents.filter(i => !settled.has(i.state)).length
  const elapsedWindow = outcome?.window_start && outcome.window_end ?
    (Date.parse(outcome.window_end) - Date.parse(outcome.window_start)) / 1000 : NaN

  return <div className="space-y-5" aria-label="Recovery overview">
    <Card className="gap-0 border-border/70 py-0 shadow-none">
      <CardContent className="p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-[10px] font-medium uppercase tracking-widest text-muted-foreground">{unresolved ? "Incident needs attention" : incident ? "Latest recorded recovery" : "Watching for incidents"}</p>
            <h2 className="mt-2 break-words text-xl font-semibold tracking-tight sm:text-2xl">{target?.name || "Waiting for service telemetry"}</h2>
            <p className="mt-1 break-all text-xs text-muted-foreground">{target ? `${target.cluster_id} / ${target.namespace}` : "Select a cluster or connect an Observer."}</p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            {incident && <><Status value={incident.state}/><Button variant="outline" size="sm" onClick={() => inspect(incident.incident_id)} aria-label={`Inspect focused incident ${incident.incident_id}`}><Search className="size-3.5"/>Inspect incident</Button></>}
          </div>
        </div>
        <div className="mt-5 grid gap-4 border-t border-border/60 pt-4 sm:grid-cols-3">
          <div><p className="text-[11px] text-muted-foreground">Observed replicas</p><p className="mt-1 flex flex-wrap items-center gap-2 text-lg font-medium">{ready}<Status value={demo && observation ? "PLAYBACK" : readiness}/></p><p className="mt-1 text-[10px] text-muted-foreground">{observation ? `Observed ${time(observation.at)}${!recent ? " / historical sample" : ""}` : "No readiness evidence received"}</p></div>
          <div><p className="text-[11px] text-muted-foreground">Recovery record</p><p className="mt-1 break-all font-mono text-xs leading-7">{incident?.incident_id || "No incident recorded"}</p><p className="mt-1 text-[10px] text-muted-foreground">{incidents.filter(i => i.state === "RECOVERED").length} verified / {incidents.length} incidents in current filters</p></div>
          <div><p className="text-[11px] text-muted-foreground">Verification evidence</p><div className="mt-2"><Status value={outcome?.status || "AWAITING OUTCOME"}/></div><p className="mt-2 text-[10px] text-muted-foreground">{outcome ? `Recorded ${time(outcome.window_end)} / not a current probe` : "An applied repair still needs a Verifier outcome."}</p></div>
        </div>
      </CardContent>
    </Card>

    <section aria-label="Focused recovery pipeline">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-medium">Recovery pipeline</h2><p className="text-[10px] text-muted-foreground">Coordinator tracks each handoff / Planner pending</p></div>
      <div className="grid overflow-hidden rounded-xl border border-border/70 bg-card sm:grid-cols-3 xl:grid-cols-6">
        {stages.map((stage, i) => <button key={stage.label} type="button" onClick={() => navigate(stage.view)} className="min-w-0 border-b border-r border-border/60 p-4 text-left transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring" aria-label={`View ${stage.label} details`}>
          <span className="flex items-center justify-between text-muted-foreground"><stage.icon className="size-4"/>{i < stages.length - 1 && <ArrowRight className="size-3"/>}</span>
          <span className="mt-3 block text-xs font-medium">{stage.label}</span><span className="mt-1 block text-[10px] text-muted-foreground">{stage.role}</span>
          <span className="mt-3 block">{stage.value}</span><span className="mt-2 block text-[10px] leading-relaxed text-muted-foreground">{stage.detail}</span>
        </button>)}
      </div>
    </section>

    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(300px,1fr)]">
      <div className="min-w-0 space-y-2"><ReadinessChart data={chart} range={range} demo={demo} telemetry={data.telemetry} target={target}/><p className="px-1 text-[10px] leading-relaxed text-muted-foreground">Replica history for {target?.name || "the focused service"}, including retained recovery rounds. Fault markers show recorded injections; a ready Pod alone does not establish verified recovery.</p></div>
      <Panel title="Recovery verification" description="Actual checks from the focused action's recorded outcome">
        {outcome ? <CardContent className="space-y-4 p-5"><div className="flex flex-wrap items-center justify-between gap-2"><Status value={outcome.status}/><span className="text-xs text-muted-foreground">{checks.filter(c => c.passed).length} / {checks.length} checks passed</span></div>
          <ul className="space-y-2.5">{checks.map((check, i) => <li key={`${check.name}-${i}`} className="flex items-start justify-between gap-3 text-xs"><span title={check.detail} className="min-w-0 leading-relaxed">{checkNames[check.name] || check.name}</span><Status value={check.passed ? "PASS" : "FAIL"}/></li>)}</ul>
          <p className="border-t border-border/60 pt-3 text-[10px] leading-relaxed text-muted-foreground">{Number.isFinite(elapsedWindow) ? `${elapsedWindow.toFixed(1)}s recorded observation window. ` : ""}Recorded outcomes do not establish current service health.</p>
          <Button variant="outline" size="sm" onClick={() => navigate("verification")}>View verification evidence<ArrowRight className="size-3"/></Button>
        </CardContent> : <Empty title="Recovery is not verified yet" detail={execution ? "Execution has a result. Waiting for the Verifier's post-action outcome." : "Checks appear after an execution result and a measured health window."}/>}
      </Panel>
    </div>

    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(300px,1fr)]">
      <Panel title="Incident timeline" description="Focused incident and active action / Coordinator record order">
        {!messages.length ? <Empty title="Waiting for recovery records" detail="Faults, detection and recovery milestones appear as producers publish evidence."/> : <ol className="divide-y divide-border/50 px-5">
          {fault && <li className="flex gap-4 py-3"><span className="mt-1.5 size-2 shrink-0 rounded-full bg-rose-400"/><div className="min-w-0 flex-1"><p className="text-xs font-medium">Fault marker recorded</p><p className="mt-1 line-clamp-3 break-words text-[11px] leading-relaxed text-muted-foreground">{fault.summary}</p></div><time className="shrink-0 font-mono text-[10px] text-muted-foreground">{time(fault.at)}</time></li>}
          {messages.map(({ sequence, message: m }) => <li key={sequence} className="flex gap-4 py-3"><span className="mt-1.5 size-2 shrink-0 rounded-full bg-primary/70"/><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><p className="text-xs font-medium">{names[m.kind] || m.kind}</p>{m.kind === "guard_result" ? <GuardDecisionStatus decision={m} demo={demo} transportStale={transportStale} now={now}/> : <Status value={m.verdict || m.status || m.action || "RECORDED"}/>}</div><p className="mt-1 line-clamp-3 break-words text-[11px] leading-relaxed text-muted-foreground">{m.rationale || m.details || m.reason_codes?.join(" / ") || (m.kind === "incident" ? m.signals?.[0]?.detail : "") || targetLabel(m.target)}</p></div><time className="shrink-0 font-mono text-[10px] text-muted-foreground">{time(stamp(m))}</time></li>)}
        </ol>}
      </Panel>
      <div className="min-w-0 space-y-5">
        <Panel title="Failure evidence" description="Selected detection signals / full records in incident details">
          {signals.length ? <CardContent className="space-y-4 p-5">{signals.slice(0, 3).map((signal, i) => <div key={i}><p className="text-xs font-medium">{signal.reason}</p><p className="mt-1 line-clamp-3 break-words text-[11px] leading-relaxed text-muted-foreground">{signal.detail}</p><p className="mt-1 text-[10px] text-muted-foreground">{signal.source}</p></div>)}{incident && <Button variant="outline" size="sm" onClick={() => inspect(incident.incident_id)}>Inspect full incident evidence<ArrowRight className="size-3"/></Button>}</CardContent> : <Empty title="No failure evidence in focus"/>}
        </Panel>
        <div className="rounded-xl border border-border/70 px-5 py-4"><p className="text-xs font-medium">Detailed workspace</p><p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">Use the sidebar for Kubernetes resources, detection evidence, approvals and the execution journal's reported results.</p><div className="mt-3 flex flex-wrap gap-2"><Button variant="outline" size="sm" onClick={() => navigate("coordinator")}>All incidents</Button><Button variant="outline" size="sm" onClick={() => navigate("cluster")}>Kubernetes</Button><Button variant="outline" size="sm" onClick={() => navigate("monitoring")}>Monitoring</Button></div></div>
      </div>
    </div>
  </div>
}
