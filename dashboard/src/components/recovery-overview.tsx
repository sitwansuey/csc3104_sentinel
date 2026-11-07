import type { ReactNode } from "react"
import { Activity, ArrowRight, ArrowUpRight, CheckCheck, GitBranch, Search, ShieldCheck, Sparkles, Terminal } from "lucide-react"
import { Button } from "@/components/ui/button"
import { CardContent } from "@/components/ui/card"
import { Empty, GuardDecisionStatus, Panel, Status, statusTone } from "@/components/records"
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
  const injectedFalseAlarm = detection?.signals?.some(signal => signal.reason === "InjectedFalseAlarm" && signal.source === "demo_harness")
  const proposal = latest("proposal") || action?.proposal
  const bound = (m?: WireMessage | null) => m && action && m.action_id === action.action_id &&
    m.incident_id === incident?.incident_id && identity(m.target) === identity(incident?.target) ? m : undefined
  const guard = latest("guard_result") || bound(action?.guard_result)
  const execution = latest("execution_result") || bound(action?.execution_result)
  const dependencies = latest("dependency_result")
  const outcome = latest("outcome")
  const rejectedWithoutExecution = guard?.verdict === "REJECT" && !execution
  const redundantRollback = guard?.reason_codes?.includes("ROLLBACK_ALREADY_AT_BASELINE")
  const report = guard?.checked_at && guard.proposal_sha256 && guard.inputs_sha256
    ? snapshot.reports.find(r => r.action_id === action?.action_id && r.checked_at === guard.checked_at &&
      r.proposal_sha256 === guard.proposal_sha256 && r.inputs_sha256 === guard.inputs_sha256) : undefined
  const rejectionReason = report?.checks.find(check => guard?.reason_codes?.includes(check.code))?.message ||
    (redundantRollback ? "The live pod template already matches the healthy baseline. This rollback would not change its configuration." :
      guard?.reason_codes?.join(" / ") || "The proposal did not pass the Guard's safety checks.")
  const executorEvidence = rejectedWithoutExecution ? [...data.telemetry].filter(e =>
    e.component === 5 && e.event_type === "status" && e.incident_id === incident?.incident_id &&
    e.action_id === action?.action_id && identity(e.target) === identity(incident?.target))
    .sort((a, b) => b.sequence - a.sequence)[0] : undefined
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
      value: <Status value={detection ? "RECORDED" : "WAITING"}/>, detail: injectedFalseAlarm ? "Harness false alarm" : "Observer evidence" },
    { label: "Propose", role: "Role 3", view: "planner", icon: Sparkles,
      value: <Status value={proposal ? "RECORDED" : "WAITING"}/>, detail: harnessProposal ? "Harness proposal" : "Submitted proposal" },
    { label: "Dependencies", role: "Role 6", view: "verification", icon: GitBranch,
      value: <Status value={dependencies?.status || "WAITING"}/>, detail: "Reviewed service graph" },
    { label: "Authorize", role: "Role 4", view: "guard", icon: ShieldCheck,
      value: guard ? <GuardDecisionStatus decision={guard} demo={demo} transportStale={transportStale} now={now}/> : <Status value="WAITING"/>, detail: "Policy and approval" },
    { label: "Execute", role: "Role 5", view: "executor", icon: Terminal,
      value: <Status value={execution?.status || (rejectedWithoutExecution ? "BLOCKED" : "WAITING")}/>, detail: rejectedWithoutExecution ? "Guard refused this proposal" : "Restricted repair" },
    { label: "Verify", role: "Role 6", view: "verification", icon: CheckCheck,
      value: <Status value={outcome?.status || (rejectedWithoutExecution ? "NOT REQUIRED" : "WAITING")}/>, detail: rejectedWithoutExecution ? "No repair scheduled" : "Measured recovery outcome" },
  ]
  const checks = outcome?.checks || []
  const signals = detection?.signals || []
  const unresolved = incidents.filter(i => !settled.has(i.state)).length
  const elapsedWindow = outcome?.window_start && outcome.window_end ?
    (Date.parse(outcome.window_end) - Date.parse(outcome.window_start)) / 1000 : NaN

  type StageState = "done" | "current" | "waiting" | "blocked" | "skipped"
  const focusSettled = incident ? settled.has(incident.state) : true
  const stageStates: StageState[] = [
    detection ? "done" : "waiting",
    proposal ? "done" : "waiting",
    dependencies ? (["BLOCKED", "FAIL", "FAILED"].includes(dependencies.status || "") ? "blocked" : "done") : "waiting",
    guard ? (guard.verdict === "REJECT" ? "blocked" : guard.verdict === "ALLOW" ? "done" : "current") : "waiting",
    execution ? (["FAILED", "CONFLICT"].includes(execution.status || "") ? "blocked" : "done") : rejectedWithoutExecution ? "skipped" : "waiting",
    outcome ? (outcome.status === "RECOVERED" ? "done" : "blocked") : rejectedWithoutExecution ? "skipped" : "waiting",
  ]
  // While an incident is in flight, the next unrecorded handoff is the one the circuit is waiting on.
  if (incident && !focusSettled) { const next = stageStates.indexOf("waiting"); if (next >= 0) stageStates[next] = "current" }
  const inFlight = Boolean(incident && !focusSettled && !transportStale)
  const tone = !incident ? "neutral" : rejectedWithoutExecution ? "warn" : !focusSettled ? "bad" : incident.state === "RECOVERED" ? "good" : "neutral"
  const passed = checks.filter(c => c.passed).length

  return <div className="stagger space-y-6" aria-label="Recovery overview">
    <section className="hero" data-tone={tone}>
      <div aria-hidden className="scope"/>
      <div className="relative p-6 sm:p-8">
        <div className="flex flex-wrap items-start justify-between gap-5">
          <div className="min-w-0 max-w-3xl">
            <p className="eyebrow bare hero-tone"><span className="beacon" data-still={tone === "good" || tone === "neutral" || undefined}/>{rejectedWithoutExecution ? "Latest rejected proposal" : unresolved ? "Incident needs attention" : incident ? "Latest recorded recovery" : "Watching for incidents"}</p>
            <h2 className="hero-name mt-5 break-words">{target?.name || "Waiting for service telemetry"}</h2>
            <p className="mt-3 break-all font-mono text-xs tracking-wide text-muted-foreground">{target ? `${target.cluster_id} / ${target.namespace}` : "Select a cluster or connect an Observer."}</p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            {incident && <><Status value={incident.state}/><Button size="sm" onClick={() => inspect(incident.incident_id)} aria-label={`Inspect focused incident ${incident.incident_id}`}><Search className="size-3.5"/>Inspect incident</Button></>}
          </div>
        </div>
        {rejectedWithoutExecution && <section aria-label="Guard rejection explanation" className="mt-6 max-w-3xl space-y-2 rounded-xl border border-warn/30 bg-warn/[.06] p-4 sm:p-5">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-warn"><ShieldCheck className="size-4"/>{redundantRollback ? "Guard rejected an unnecessary rollback" : "Guard rejected the proposal"}</h3>
          <p className="text-xs leading-relaxed text-muted-foreground">{rejectionReason}</p>
          <p className="text-xs font-medium">No execution scheduled for this proposal. No cluster modification was authorized.</p>
          {executorEvidence && <div className="border-t border-border pt-2"><p className="font-mono text-[10px] uppercase tracking-[.14em] text-muted-foreground">Recorded Executor evidence</p><p className="mt-1 text-xs leading-relaxed">{executorEvidence.summary}</p></div>}
        </section>}
        <div className="mt-8 grid border-t border-border sm:grid-cols-3">
          <div className="readout"><p className="console-label">Observed replicas</p><p className="mt-2 flex flex-wrap items-center gap-3"><span className="tile-value text-[2.1rem]">{ready}</span><Status value={demo && observation ? "PLAYBACK" : readiness}/></p><p className="mt-2 text-[11px] text-muted-foreground">{observation ? `Observed ${time(observation.at)}${!recent ? " / historical sample" : ""}` : "No readiness evidence received"}</p></div>
          <div className="readout"><p className="console-label">Recovery record</p><p className="mt-3 break-all font-mono text-sm leading-7">{incident?.incident_id || "No incident recorded"}</p><p className="mt-1.5 text-[11px] text-muted-foreground">{incidents.filter(i => i.state === "RECOVERED").length} verified / {incidents.length} incidents in current filters</p></div>
          <div className="readout"><p className="console-label">Verification evidence</p><div className="mt-3"><Status value={outcome?.status || (rejectedWithoutExecution ? "NOT REQUIRED" : "AWAITING OUTCOME")}/></div><p className="mt-2.5 text-[11px] leading-relaxed text-muted-foreground">{outcome ? `Recorded ${time(outcome.window_end)} / not a current probe` : rejectedWithoutExecution ? "The proposal was rejected before execution; no recovery outcome is expected." : "An applied repair still needs a Verifier outcome."}</p></div>
        </div>
      </div>
    </section>

    <section aria-label="Focused recovery pipeline">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-2"><div><p className="eyebrow">Recovery circuit</p><h2 className="mt-2.5 text-lg font-semibold tracking-tight">Recovery pipeline</h2></div><p className="font-mono text-[10px] tracking-wide text-muted-foreground">Coordinator tracks each handoff / Planner pending</p></div>
      <div className="circuit sm:grid-cols-3 xl:grid-cols-6" data-live={inFlight || undefined}>
        {stages.map((stage, i) => <button key={stage.label} type="button" data-state={stageStates[i]} onClick={() => navigate(stage.view)} className="station" aria-label={`View ${stage.label} details`}>
          <span aria-hidden className="station-bar"/><span aria-hidden className="station-node"/>
          <span className="flex items-center justify-between font-mono text-[10px] tracking-[.18em] text-muted-foreground"><span>{String(i + 1).padStart(2, "0")}</span><stage.icon className="station-icon size-4"/></span>
          <span className="station-label">{stage.label}</span>
          <span className="mt-1.5 block font-mono text-[10px] uppercase tracking-[.14em] text-muted-foreground">{stage.role}</span>
          <span className="mt-4 block">{stage.value}</span>
          <span className="mt-2.5 block text-[11px] leading-relaxed text-muted-foreground">{stage.detail}</span>
        </button>)}
      </div>
    </section>

    <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1.6fr)_minmax(300px,1fr)]">
      <div className="min-w-0 space-y-2"><ReadinessChart data={chart} range={range} demo={demo} telemetry={data.telemetry} target={target}/><p className="px-1 text-[10.5px] leading-relaxed text-muted-foreground">Replica history for {target?.name || "the focused service"}, including retained recovery rounds. Fault markers show recorded injections; a ready Pod alone does not establish verified recovery.</p></div>
      <Panel title="Recovery verification" description="Actual checks from the focused action's recorded outcome">
        {outcome ? <CardContent className="space-y-5 p-5"><div className="flex flex-wrap items-center justify-between gap-2"><Status value={outcome.status}/><span className="font-mono text-[11px] text-muted-foreground">{`${passed} / ${checks.length} checks passed`}</span></div>
          <div className="meter" data-tone={checks.length && passed === checks.length ? undefined : "bad"} aria-hidden><span style={{ transform: `scaleX(${checks.length ? passed / checks.length : 0})` }}/></div>
          <ul className="space-y-1">{checks.map((check, i) => <li key={`${check.name}-${i}`} className="flex items-start justify-between gap-3 border-b border-border/60 py-2.5 text-xs last:border-0"><span title={check.detail} className="min-w-0 leading-relaxed">{checkNames[check.name] || check.name}</span><Status value={check.passed ? "PASS" : "FAIL"}/></li>)}</ul>
          <p className="border-t border-border pt-3 text-[10.5px] leading-relaxed text-muted-foreground">{Number.isFinite(elapsedWindow) ? `${elapsedWindow.toFixed(1)}s recorded observation window. ` : ""}Recorded outcomes do not establish current service health.</p>
          <Button variant="outline" size="sm" className="border-border bg-transparent dark:bg-transparent" onClick={() => navigate("verification")}>View verification evidence<ArrowRight className="size-3"/></Button>
        </CardContent> : rejectedWithoutExecution ? <Empty title="No post-repair verification required" detail="The Guard rejected this proposal and no execution was scheduled. Observed replicas remain a separate health signal."/> : <Empty title="Recovery is not verified yet" detail={execution ? "Execution has a result. Waiting for the Verifier's post-action outcome." : "Checks appear after an execution result and a measured health window."}/>}
      </Panel>
    </div>

    <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1.6fr)_minmax(300px,1fr)]">
      <Panel title="Incident timeline" description="Focused incident and active action / Coordinator record order">
        {!messages.length ? <Empty title="Waiting for recovery records" detail="Faults, detection and recovery milestones appear as producers publish evidence."/> : <ol className="timeline px-5 py-2">
          {fault && <li className="flex gap-4 py-3.5"><span className="tl-node" data-tone="bad"/><div className="min-w-0 flex-1"><p className="text-xs font-medium text-bad">Fault marker recorded</p><p className="mt-1 line-clamp-3 break-words text-[11px] leading-relaxed text-muted-foreground">{fault.summary}</p></div><time className="shrink-0 font-mono text-[10px] text-muted-foreground">{time(fault.at)}</time></li>}
          {messages.map(({ sequence, message: m }) => { const recorded = statusTone(m.verdict || m.status); return <li key={sequence} className="flex gap-4 py-3.5"><span className="tl-node" data-tone={recorded === "good" || recorded === "bad" ? recorded : undefined}/><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><p className="text-xs font-medium">{m.kind === "incident" && injectedFalseAlarm ? "False alarm submitted" : names[m.kind] || m.kind}</p>{m.kind === "guard_result" ? <GuardDecisionStatus decision={m} demo={demo} transportStale={transportStale} now={now}/> : <Status value={m.verdict || m.status || m.action || "RECORDED"}/>}</div><p className="mt-1 line-clamp-3 break-words text-[11px] leading-relaxed text-muted-foreground">{m.rationale || m.details || m.reason_codes?.join(" / ") || (m.kind === "incident" ? m.signals?.[0]?.detail : "") || targetLabel(m.target)}</p></div><time className="shrink-0 font-mono text-[10px] text-muted-foreground">{time(stamp(m))}</time></li> })}
        </ol>}
      </Panel>
      <div className="min-w-0 space-y-6">
        <Panel title={injectedFalseAlarm ? "Injected false alarm evidence" : "Failure evidence"} description="Selected detection signals / full records in incident details">
          {signals.length ? <CardContent className="space-y-4 p-5">{signals.slice(0, 3).map((signal, i) => <div key={i} className="border-l-2 border-bad/60 pl-3"><p className="text-xs font-semibold">{signal.reason}</p><p className="mt-1 line-clamp-3 break-words text-[11px] leading-relaxed text-muted-foreground">{signal.detail}</p><p className="mt-1 font-mono text-[10px] text-muted-foreground/80">{signal.source}</p></div>)}{incident && <Button variant="outline" size="sm" className="border-border bg-transparent dark:bg-transparent" onClick={() => inspect(incident.incident_id)}>Inspect full incident evidence<ArrowRight className="size-3"/></Button>}</CardContent> : <Empty title="No failure evidence in focus"/>}
        </Panel>
        <div className="tile"><p className="console-label">Detailed workspace</p><p className="mt-3 text-[11.5px] leading-relaxed text-muted-foreground">Use the sidebar for Kubernetes resources, detection evidence, approvals and the execution journal's reported results.</p><div className="mt-4 flex flex-wrap gap-2">{([["coordinator", "All incidents"], ["cluster", "Kubernetes"], ["monitoring", "Monitoring"]] as [View, string][]).map(([id, label]) => <Button key={id} variant="outline" size="sm" className="border-border bg-transparent dark:bg-transparent" onClick={() => navigate(id)}>{label}<ArrowUpRight className="size-3 opacity-60"/></Button>)}</div></div>
      </div>
    </div>
  </div>
}
