import { useState, type ReactNode } from "react"
import { Activity, ArrowRight, FileSearch, Layers3, LockKeyhole } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Fields, Status } from "@/components/records"
import type { Action, Snapshot, Target, WireMessage } from "@/lib/types"

interface IncidentDetailsProps {
  snapshot: Snapshot
  incidentId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

const sameTarget = (left: Target, right: Target) =>
  left.cluster_id === right.cluster_id && left.namespace === right.namespace &&
  left.kind === right.kind && left.name === right.name && left.uid === right.uid

function timestamp(value?: string | null) {
  if (!value) return "No producer timestamp"
  const date = new Date(value)
  return Number.isNaN(+date) ? "Invalid producer timestamp" : date.toLocaleString()
}

const messageTimestamp = (message: WireMessage) =>
  message.checked_at || message.recorded_at || message.observed_at || message.window_end

function DetailSection({ title, member, children }: { title: string; member?: string; children: ReactNode }) {
  return <Card className="gap-0 overflow-hidden border-border/70 py-0 shadow-none">
    <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 border-b border-border/50 px-4 py-3">
      <CardTitle className="text-sm font-medium">{title}</CardTitle>
      {member && <Badge variant="secondary" className="text-[10px] font-normal">{member}</Badge>}
    </CardHeader>
    <CardContent className="space-y-4 p-4">{children}</CardContent>
  </Card>
}

function AwaitingRecord({ detail }: { detail: string }) {
  return <div className="flex items-start gap-3 rounded-lg bg-muted/30 p-3 text-xs">
    <FileSearch className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
    <div><p className="font-medium">Awaiting record</p><p className="mt-1 leading-relaxed text-muted-foreground">{detail} The result is unknown.</p></div>
  </div>
}

function RecordedJson({ value, label = "Inspect recorded JSON" }: { value: unknown; label?: string }) {
  const text = JSON.stringify(value, null, 2)
  const limit = 16000
  return <details className="min-w-0 rounded-lg border border-border/60">
    <summary className="cursor-pointer px-3 py-2.5 text-xs text-muted-foreground">{label}</summary>
    <pre className="max-h-72 overflow-auto border-t bg-muted/20 p-3 text-[11px] leading-relaxed">{text.slice(0, limit)}{text.length > limit ? "\n… Preview truncated; full record remains at the source." : ""}</pre>
  </details>
}

function TextList({ values, empty }: { values?: string[]; empty: string }) {
  if (!values?.length) return <p className="text-xs text-muted-foreground">{empty}</p>
  return <ul className="space-y-1.5 text-xs leading-relaxed">{values.slice(0, 30).map((value, i) => <li key={i} className="break-words">{value}</li>)}{values.length > 30 && <li className="text-muted-foreground">Showing 30 of {values.length} recorded entries.</li>}</ul>
}

const phaseNames: Record<string, string> = {
  incident: "Failure detected", proposal: "Recovery proposed", guard_result: "Guard decision",
  dependency_result: "Dependencies checked", execution_result: "Execution recorded", outcome: "Outcome verified",
}
const phaseLabel = (message: WireMessage) => message.kind === "incident" && message.signals?.some(signal =>
  signal.reason === "InjectedFalseAlarm" && signal.source === "demo_harness") ? "False alarm submitted" : phaseNames[message.kind] || message.kind

export function IncidentDetails({ snapshot, incidentId, open, onOpenChange }: IncidentDetailsProps) {
  const [selection, setSelection] = useState<{ incidentId: string; actionId: string } | null>(null)
  const incident = snapshot.incidents.find(record => record.incident_id === incidentId)
  const incidentMessages = incident ? snapshot.messages.filter(({ message }) =>
    message.incident_id === incident.incident_id && sameTarget(message.target, incident.target)) : []
  // The persisted sequence is authoritative for display order. Producer times are descriptive only.
  const orderedMessages = [...incidentMessages].sort((left, right) => left.sequence - right.sequence)
  const actions = incident ? snapshot.actions.filter(action => action.incident_id === incident.incident_id &&
    action.proposal.incident_id === incident.incident_id && action.proposal.action_id === action.action_id &&
    sameTarget(action.proposal.target, incident.target)) : []
  const proposalSequence = (action: Action) => orderedMessages.filter(({ message }) =>
    message.kind === "proposal" && message.action_id === action.action_id).at(-1)?.sequence ?? -1
  const latestAction = [...actions].sort((left, right) => proposalSequence(right) - proposalSequence(left))[0]
  const defaultAction = actions.find(action => action.action_id === incident?.active_action_id) || latestAction
  const selectedAction = selection && selection.incidentId === incident?.incident_id
    ? actions.find(action => action.action_id === selection.actionId) || defaultAction : defaultAction
  const actionMatches = (message: WireMessage) => Boolean(incident && selectedAction &&
    message.incident_id === incident.incident_id && message.action_id === selectedAction.action_id &&
    sameTarget(message.target, incident.target))
  const selectedMessages = orderedMessages.filter(({ message }) => message.kind === "incident" || actionMatches(message))
  const latestMessage = (kind: string) => selectedMessages.filter(({ message }) => message.kind === kind && actionMatches(message)).at(-1)?.message
  const proposal = selectedAction?.proposal
  const guard = latestMessage("guard_result") || (selectedAction?.guard_result && actionMatches(selectedAction.guard_result) ? selectedAction.guard_result : undefined)
  const execution = latestMessage("execution_result") || (selectedAction?.execution_result && actionMatches(selectedAction.execution_result) ? selectedAction.execution_result : undefined)
  const dependencies = latestMessage("dependency_result")
  const outcome = latestMessage("outcome")
  const rejectedWithoutExecution = guard?.verdict === "REJECT" && !execution
  const executorEvidence = rejectedWithoutExecution ? [...snapshot.telemetry].filter(record =>
    record.component === 5 && record.event_type === "status" && record.incident_id === incident?.incident_id &&
    record.action_id === selectedAction?.action_id && record.target && incident && sameTarget(record.target, incident.target))
    .sort((left, right) => right.sequence - left.sequence)[0] : undefined
  const detections = orderedMessages.filter(({ message }) => message.kind === "incident")
  const report = guard?.checked_at && guard.proposal_sha256 && guard.inputs_sha256
    ? snapshot.reports.find(record => record.action_id === selectedAction?.action_id && record.checked_at === guard.checked_at &&
      record.proposal_sha256 === guard.proposal_sha256 && record.inputs_sha256 === guard.inputs_sha256) : undefined
  const synthetic = snapshot.mode === "synthetic"
  const referenceTime = synthetic ? Date.parse(snapshot.generated_at) : Date.now()
  const expiresAt = Date.parse(guard?.valid_until || "")
  const expired = Number.isFinite(referenceTime) && Number.isFinite(expiresAt) && expiresAt <= referenceTime

  return <Sheet open={open} onOpenChange={onOpenChange}>
    <SheetContent className="w-full gap-0 overflow-hidden sm:max-w-[760px] lg:max-w-[880px]">
      <SheetHeader className="shrink-0 border-b px-5 pb-4 pt-5 pr-12 sm:px-6 sm:pr-12">
        <div className="mb-1 flex items-center gap-2 text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground"><Layers3 className="size-3.5" /> Incident workspace</div>
        <SheetTitle className="break-all text-xl tracking-tight">{incidentId || "Incident details"}</SheetTitle>
        <SheetDescription>Follow the recorded detection, proposal, Guard decision, execution and verification for one incident.</SheetDescription>
      </SheetHeader>
      <div className="min-h-0 flex-1 space-y-5 overflow-x-hidden overflow-y-auto px-5 py-5 sm:px-6">
        {!incident ? <AwaitingRecord detail="This incident is unavailable in the current bounded snapshot. It may have moved outside the record window." /> : <>
          <div className="flex items-start gap-2.5 rounded-xl border bg-muted/20 p-3.5 text-xs leading-relaxed text-muted-foreground">
            <LockKeyhole className="mt-0.5 size-4 shrink-0" /><p>{synthetic ? "Fixture clock / synthetic. All records illustrate the workflow and provide no execution authority. " : "Recorded history is read-only and does not grant execution permission. "}This drawer does not approve actions or consume execution requests.</p>
          </div>
          <DetailSection title="Target & lifecycle" member="Member 1 · Coordinator">
            <div className="flex flex-wrap items-center gap-2"><Status value={incident.state} /><span className="text-xs text-muted-foreground">Recorded incident lifecycle</span></div>
            <Fields entries={[["Cluster", incident.target.cluster_id], ["Namespace", incident.target.namespace], ["Workload", `${incident.target.kind} / ${incident.target.name}`], ["Workload UID", incident.target.uid], ["Active action", incident.active_action_id || "No active action recorded"]]} />
            {actions.length > 1 && <div className="space-y-2"><label htmlFor="incident-action" className="text-xs font-medium">Inspect recovery action</label><Select value={selectedAction?.action_id} onValueChange={actionId => setSelection({ incidentId: incident.incident_id, actionId })}>
              <SelectTrigger id="incident-action" aria-label="Incident action" className="w-full"><SelectValue placeholder="Choose an action" /></SelectTrigger>
              <SelectContent>{actions.map(action => <SelectItem key={action.action_id} value={action.action_id}>{action.action_id}{action.action_id === incident.active_action_id ? " · active" : ""}</SelectItem>)}</SelectContent>
            </Select></div>}
            {selectedAction && <div className="flex flex-wrap items-center gap-2 border-t pt-3 text-xs"><span className="font-mono">{selectedAction.action_id}</span><ArrowRight className="size-3 text-muted-foreground" /><Status value={selectedAction.state} /><span className="text-muted-foreground">Recorded action state</span></div>}
          </DetailSection>
          <DetailSection title="Detection evidence" member="Member 2 · Monitoring">
            {detections.length ? detections.slice(-5).map(({ sequence, message }) => <div key={sequence} className="space-y-3">
              <p className="text-[11px] text-muted-foreground">Observed: {timestamp(message.observed_at)}</p>
              {message.signals?.length ? <div className="space-y-2">{message.signals.slice(0, 30).map((signal, i) => <div key={i} className="rounded-lg border border-border/60 p-3 text-xs"><p className="break-words font-medium">{signal.reason}</p><p className="mt-1 break-words leading-relaxed text-muted-foreground">{signal.detail}</p><p className="mt-2 text-[10px] text-muted-foreground">{signal.source} · {timestamp(signal.at)}</p></div>)}</div> : <p className="text-xs text-muted-foreground">No detection signals were included in this incident record.</p>}
            </div>) : <AwaitingRecord detail="No detection message for this exact incident and workload is available." />}
          </DetailSection>
          <DetailSection title="Recovery proposal" member="Member 3 · Planner / RCA">
            {proposal ? <><Fields entries={[["Action", proposal.action], ["Based on version", proposal.based_on_resource_version], ["Producer time", timestamp(messageTimestamp(proposal))]]} />
              <p className="break-words text-sm leading-relaxed">{proposal.rationale || "No rationale recorded."}</p>
              <div><p className="mb-2 text-xs font-medium">Evidence references</p><TextList values={proposal.evidence_refs} empty="No evidence references recorded." /><p className="mt-2 text-[11px] text-muted-foreground">References are displayed as supplied; this dashboard does not resolve or validate their contents.</p></div>
              <RecordedJson value={proposal} label="Inspect proposal & parameters" />
            </> : <AwaitingRecord detail="No matching recovery proposal has been recorded." />}
          </DetailSection>
          <DetailSection title="Guard evaluation" member="Member 4 · Recovery Guard">
            {guard ? <>
              <div className="flex flex-wrap items-center gap-2">{guard.verdict === "ALLOW" && (expired || synthetic) ? <Badge variant="outline" className="font-mono text-[10px]">ALLOW · recorded</Badge> : <Status value={guard.verdict} />}{expired && <Badge variant="secondary">Expired decision</Badge>}</div>
              <p className="text-xs leading-relaxed text-muted-foreground">{expired ? "This historical decision has expired. It cannot authorize a new execution." : "This is a recorded verdict. The Executor must revalidate current state and decision bindings before any write."}</p>
              <Fields entries={[["Checked", timestamp(guard.checked_at)], ["Valid until", timestamp(guard.valid_until)], ["Policy version", guard.policy_version], ["Validated version", guard.validated_resource_version]]} />
              <div><p className="mb-2 text-xs font-medium">Recorded reason codes</p><TextList values={guard.reason_codes} empty="No reason codes recorded." /></div>
              {report ? <div className="space-y-2"><p className="text-xs font-medium">Bound findings</p>{report.checks.slice(0, 80).map((finding, i) => <div key={i} className="rounded-lg border border-border/60 p-3 text-xs"><div className="flex flex-wrap items-start justify-between gap-2"><span className="min-w-0 break-all font-mono text-[11px]">{finding.code}</span><Status value={finding.status} /></div><p className="mt-2 break-words leading-relaxed text-muted-foreground">{finding.message}</p><p className="mt-2 break-all font-mono text-[10px] text-muted-foreground">{finding.path}</p></div>)}{report.checks.length > 80 && <p className="text-xs text-muted-foreground">Showing 80 of {report.checks.length} findings.</p>}<RecordedJson value={report} label="Inspect matching findings report" /></div> : <p className="rounded-lg bg-muted/30 p-3 text-xs leading-relaxed text-muted-foreground">No matching detailed report. Findings must match this action, decision time, proposal hash and input hash.</p>}
              <RecordedJson value={guard} label="Inspect recorded Guard decision" />
            </> : <AwaitingRecord detail="No Guard decision matching the selected action and workload has been recorded." />}
          </DetailSection>
          <DetailSection title="Execution" member="Member 5 · Restricted Executor">
            {execution ? <><Status value={execution.status} /><Fields entries={[["Recorded", timestamp(execution.recorded_at)], ["Resulting version", execution.resource_version]]} /><p className="break-words text-xs leading-relaxed text-muted-foreground">{execution.details || "No execution detail recorded."}</p><RecordedJson value={execution} /></> : rejectedWithoutExecution ? <><Status value="BLOCKED" /><p className="text-xs leading-relaxed text-muted-foreground">No execution scheduled: the Guard rejected this proposal. No cluster modification was authorized for this proposal.</p>{executorEvidence && <div className="rounded-lg border border-border/60 p-3 text-xs"><p className="font-medium">Recorded Executor evidence</p><p className="mt-2 leading-relaxed text-muted-foreground">{executorEvidence.summary}</p></div>}</> : <AwaitingRecord detail="No execution result for the selected action has been recorded. This does not imply that a write succeeded or failed." />}
          </DetailSection>
          <DetailSection title="Dependencies before execution" member="Member 6 · Dependencies">
            {dependencies ? <><Status value={dependencies.status} /><p className="text-[11px] text-muted-foreground">Checked: {timestamp(dependencies.checked_at)}</p><div className="grid gap-4 sm:grid-cols-2"><div><p className="mb-2 text-xs font-medium">Blockers</p><TextList values={dependencies.blockers} empty="No blockers listed in this record." /></div><div><p className="mb-2 text-xs font-medium">Unknowns</p><TextList values={dependencies.unknowns} empty="No unknowns listed in this record." /></div></div><div><p className="mb-2 text-xs font-medium">Affected services</p><TextList values={dependencies.affected_services} empty="No affected services listed." /></div><RecordedJson value={dependencies} /></> : <AwaitingRecord detail="No dependency assessment for the selected action has been recorded." />}
          </DetailSection>
          <DetailSection title="Outcome after execution" member="Member 6 · Verification">
            {outcome ? <><Status value={outcome.status} /><p className="text-xs leading-relaxed text-muted-foreground">This is the recorded result for the verification window, not a statement of current workload health.</p><Fields entries={[["Window start", timestamp(outcome.window_start)], ["Window end", timestamp(outcome.window_end)]]} />{outcome.checks?.map((check, i) => <div key={i} className="flex items-start gap-3 rounded-lg border border-border/60 p-3 text-xs"><Status value={check.passed ? "PASS" : "FAIL"} /><div className="min-w-0"><p className="break-words font-medium">{check.name}</p><p className="mt-1 break-words leading-relaxed text-muted-foreground">{check.detail}</p></div></div>)}<RecordedJson value={outcome} /></> : rejectedWithoutExecution ? <><Status value="NOT REQUIRED" /><p className="text-xs leading-relaxed text-muted-foreground">No post-repair verification required: this proposal was rejected before execution. No recovery outcome is expected.</p></> : <AwaitingRecord detail="No post-execution verification for the selected action has been recorded." />}
          </DetailSection>
          <DetailSection title="Stored event sequence">
            <p className="text-xs leading-relaxed text-muted-foreground">Ordered by Coordinator database sequence, oldest first. Producer timestamps may be absent or describe a different stage; elapsed recovery time is not inferred.</p>
            {selectedMessages.length ? <ol aria-label="Stored incident event sequence" className="space-y-0">{selectedMessages.slice(-60).map(({ sequence, message }) => <li key={sequence} className="flex gap-3 border-b border-border/40 py-3 last:border-0"><div className="mt-0.5 rounded-md bg-muted/60 p-1.5"><Activity className="size-3.5 text-muted-foreground" /></div><div className="min-w-0 flex-1"><div className="flex flex-wrap justify-between gap-2"><p className="text-xs font-medium">{phaseLabel(message)}</p><span className="font-mono text-[10px] text-muted-foreground">#{sequence}</span></div><p className="mt-1 text-[11px] text-muted-foreground">{timestamp(messageTimestamp(message))}</p></div></li>)}</ol> : <AwaitingRecord detail="No stored messages for this incident and action are available." />}
            {selectedMessages.length > 60 && <p className="text-xs text-muted-foreground">Showing the latest 60 of {selectedMessages.length} matching messages.</p>}
          </DetailSection>
        </>}
      </div>
    </SheetContent>
  </Sheet>
}
