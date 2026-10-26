import { render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"
import { RecoveryOverview } from "./recovery-overview"
import { TooltipProvider } from "./ui/tooltip"
import { scope } from "@/lib/data"
import { fixture } from "@/test/fixtures"
import type { Snapshot } from "@/lib/types"

function show(snapshot: Snapshot, transportStale = false) {
  return render(<TooltipProvider><RecoveryOverview snapshot={snapshot} data={scope(snapshot, "", "")}
    range="all" now={Date.now()} transportStale={transportStale} navigate={vi.fn()} inspect={vi.fn()}/></TooltipProvider>)
}

function rejectedProposal() {
  const s = fixture()
  s.incidents[0].state = "REJECTED"
  s.actions[0].state = "REJECTED"
  s.actions[0].execution_result = null
  s.messages = s.messages.filter(r => !["execution_result", "outcome"].includes(r.message.kind))
  const guard = s.actions[0].guard_result!
  guard.verdict = "REJECT"
  guard.reason_codes = ["ROLLBACK_ALREADY_AT_BASELINE"]
  s.outbox = []
  s.reports[0].checks = [{ code: "ROLLBACK_ALREADY_AT_BASELINE", status: "FAIL", path: "live.template",
    message: "Recorded finding: live configuration already equals the approved baseline.", actual: "same", expected: "different" }]
  const detection = s.messages.find(r => r.message.kind === "incident")!.message
  detection.signals = [{ reason: "InjectedFalseAlarm", source: "demo_harness",
    detail: "Injected false alarm: healthy configuration; test a redundant rollback.", at: s.generated_at }]
  return s
}

describe("focused recovery Overview", () => {
  it("shows real outcome checks and keeps resource usage off the Overview", () => {
    show(fixture())
    expect(screen.getByText("Application response and replicas")).toBeInTheDocument()
    expect(screen.getByText("1 / 1 checks passed")).toBeInTheDocument()
    expect(screen.getByText("Deployment readiness")).toBeInTheDocument()
    expect(screen.queryByText("Resource utilization")).not.toBeInTheDocument()
    expect(screen.queryByText("CPU usage")).not.toBeInTheDocument()
    const pipeline = screen.getByRole("region", { name: "Focused recovery pipeline" })
    expect(within(within(pipeline).getByRole("button", { name: "View Execute details" })).getByText("APPLIED")).toBeInTheDocument()
    expect(within(within(pipeline).getByRole("button", { name: "View Verify details" })).getByText("RECOVERED")).toBeInTheDocument()
    expect(screen.queryByText("No post-repair verification required")).not.toBeInTheDocument()
  })

  it("explains a rejected false alarm without implying a repair or pending verification", () => {
    const s = rejectedProposal()
    s.telemetry = []
    show(s)
    expect(screen.getByText("Latest rejected proposal")).toBeInTheDocument()
    expect(screen.getByText("Guard rejected an unnecessary rollback")).toBeInTheDocument()
    expect(screen.getByText("Recorded finding: live configuration already equals the approved baseline.")).toBeInTheDocument()
    expect(screen.getByText(/No execution scheduled for this proposal/)).toBeInTheDocument()
    expect(screen.getByText("No post-repair verification required")).toBeInTheDocument()
    expect(screen.queryByText("Recovery is not verified yet")).not.toBeInTheDocument()
    expect(screen.getByText("False alarm submitted")).toBeInTheDocument()
    expect(screen.queryByText("Failure detected")).not.toBeInTheDocument()
    expect(screen.getByText("No observations")).toBeInTheDocument()
    expect(screen.queryByText("Ready", { exact: true })).not.toBeInTheDocument()
    expect(screen.queryByText(/zero patch attempts/i)).not.toBeInTheDocument()
    const pipeline = screen.getByRole("region", { name: "Focused recovery pipeline" })
    expect(within(within(pipeline).getByRole("button", { name: "View Execute details" })).getByText("BLOCKED")).toBeInTheDocument()
    expect(within(within(pipeline).getByRole("button", { name: "View Verify details" })).getByText("NOT REQUIRED")).toBeInTheDocument()
  })

  it.each(["checked_at", "proposal_sha256", "inputs_sha256"] as const)("uses the reason-code explanation when a detailed finding has mismatching %s", field => {
    const s = rejectedProposal()
    s.reports[0][field] = "wrong decision binding"
    show(s)
    expect(screen.queryByText("Recorded finding: live configuration already equals the approved baseline.")).not.toBeInTheDocument()
    expect(screen.getByText(/live pod template already matches the healthy baseline/)).toBeInTheDocument()
  })

  it("shows zero-write evidence only from an Executor event bound to the focused incident, action and target", () => {
    const s = rejectedProposal()
    const evidence = { sequence: 20, at: s.generated_at, component: 5, event_type: "status", summary: "Independent Executor test: zero patch attempts; generation and Pod UIDs unchanged.",
      target: s.incidents[0].target, incident_id: s.incidents[0].incident_id, action_id: s.actions[0].action_id, metrics: {} }
    s.telemetry.push(evidence, { ...evidence, sequence: 21, action_id: "OTHER", summary: "FOREIGN_ACTION_EVIDENCE" },
      { ...evidence, sequence: 22, incident_id: "OTHER", summary: "FOREIGN_INCIDENT_EVIDENCE" },
      { ...evidence, sequence: 23, target: { ...evidence.target, uid: "OTHER" }, summary: "FOREIGN_TARGET_EVIDENCE" })
    show(s)
    expect(screen.getByText(evidence.summary)).toBeInTheDocument()
    expect(screen.queryByText(/FOREIGN_.*_EVIDENCE/)).not.toBeInTheDocument()
  })

  it("preserves recorded execution and verification when a rejection is also present", () => {
    const s = fixture()
    s.actions[0].guard_result!.verdict = "REJECT"
    show(s)
    expect(screen.getByText("Application response and replicas")).toBeInTheDocument()
    expect(screen.queryByText(/No execution scheduled for this proposal/)).not.toBeInTheDocument()
    const pipeline = screen.getByRole("region", { name: "Focused recovery pipeline" })
    expect(within(within(pipeline).getByRole("button", { name: "View Execute details" })).getByText("APPLIED")).toBeInTheDocument()
  })

  it("prioritizes an unresolved incident without borrowing the previous recovery", () => {
    const s = fixture()
    s.incidents.unshift({ ...s.incidents[0], sequence: 2, incident_id: "INC-NEW", state: "OPEN", active_action_id: null })
    show(s)
    expect(screen.getByText("INC-NEW")).toBeInTheDocument()
    expect(screen.getByText("Recovery is not verified yet")).toBeInTheDocument()
    expect(screen.queryByText("RECOVERED")).not.toBeInTheDocument()
    expect(screen.queryByText("Application response and replicas")).not.toBeInTheDocument()
  })

  it("does not treat clear dependencies or a previous action's outcome as health verification", () => {
    const s = fixture()
    const action = structuredClone(s.actions[0])
    action.action_id = "ACT-NEW"
    action.proposal.action_id = action.action_id
    action.proposal.rationale = "Operator-authored live lab proposal"
    action.state = "VERIFYING"
    action.guard_result = null
    action.execution_result = null
    s.actions.unshift(action)
    s.incidents[0].active_action_id = action.action_id
    s.incidents[0].state = "VERIFYING"
    const dependency = structuredClone(s.messages.find(r => r.message.kind === "dependency_result")!)
    dependency.sequence = 10
    dependency.message.action_id = action.action_id
    s.messages.unshift(dependency)
    show(s)
    const pipeline = screen.getByRole("region", { name: "Focused recovery pipeline" })
    expect(within(pipeline).getByText("CLEAR")).toBeInTheDocument()
    expect(within(pipeline).getByText("Harness proposal")).toBeInTheDocument()
    expect(within(within(pipeline).getByRole("button", { name: "View Verify details" })).getByText("WAITING")).toBeInTheDocument()
    expect(screen.getByText("Recovery is not verified yet")).toBeInTheDocument()
    expect(screen.queryByText("RECOVERED")).not.toBeInTheDocument()
  })

  it("does not associate an outcome with the wrong target UID", () => {
    const s = fixture()
    s.messages.find(r => r.message.kind === "outcome")!.message.target = { ...s.incidents[0].target, uid: "replacement-deployment" }
    show(s)
    expect(screen.getByText("Recovery is not verified yet")).toBeInTheDocument()
    expect(screen.queryByText("Application response and replicas")).not.toBeInTheDocument()
  })

  it("labels disconnected healthy replica observations as historical", () => {
    const s = fixture()
    s.mode = "recorded/live producers"
    show(s, true)
    expect(screen.getByText("Last known")).toBeInTheDocument()
    expect(screen.getByText(/historical sample/)).toBeInTheDocument()
    expect(screen.queryByText("Ready", { exact: true })).not.toBeInTheDocument()
    expect(screen.getByText(/not a current probe/)).toBeInTheDocument()
  })

  it("shows waiting states instead of inventing readiness when no producer records exist", () => {
    const s = fixture()
    s.incidents = []; s.actions = []; s.messages = []; s.telemetry = []; s.cluster_history = []
    show(s)
    expect(screen.getByText("Waiting for service telemetry")).toBeInTheDocument()
    expect(screen.getByText("No observations")).toBeInTheDocument()
    expect(screen.getByText("Waiting for recovery records")).toBeInTheDocument()
    expect(screen.queryByText("RECOVERED")).not.toBeInTheDocument()
  })
})
