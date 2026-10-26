import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import { IncidentDetails } from "./incident-details"
import { fixture } from "@/test/fixtures"
import type { Snapshot, WireMessage } from "@/lib/types"

function addAction(snapshot: Snapshot, actionId = "ACT-002") {
  const proposal = { ...structuredClone(snapshot.actions[0].proposal), action_id: actionId, rationale: `Proposal for ${actionId}` }
  const guard: WireMessage = { ...structuredClone(snapshot.actions[0].guard_result!), action_id: actionId, verdict: "REJECT", reason_codes: [`REASON_${actionId}`] }
  snapshot.actions.push({ action_id: actionId, incident_id: "INC-001", proposal, guard_result: guard, execution_result: null, state: "REJECTED" })
  snapshot.messages.push({ sequence: 20, message: proposal }, { sequence: 21, message: guard })
}

function details(snapshot = fixture(), incidentId = "INC-001") {
  return render(<IncidentDetails snapshot={snapshot} incidentId={incidentId} open onOpenChange={vi.fn()} />)
}

describe("incident detail workspace", () => {
  it("combines the exact incident and selected action without leaking other actions or targets", async () => {
    const data = fixture()
    addAction(data)
    const foreign = { ...structuredClone(data.actions[0].execution_result!), incident_id: "INC-OTHER", details: "FOREIGN_INCIDENT_CONTENT" }
    const wrongTarget = { ...structuredClone(data.actions[0].execution_result!), target: { ...data.incidents[0].target, uid: "recreated-uid" }, details: "FOREIGN_TARGET_CONTENT" }
    data.messages.push({ sequence: 50, message: foreign }, { sequence: 51, message: wrongTarget })
    details(data)
    expect(screen.getByRole("dialog", { name: "INC-001" })).toBeInTheDocument()
    expect(screen.getByText("Persistent restart failure")).toBeInTheDocument()
    expect(screen.getByText("Restore the observed healthy template")).toBeInTheDocument()
    expect(screen.getByText("BASELINE_HEALTH_STATUS")).toBeInTheDocument()
    expect(screen.getByText("Synthetic execution result")).toBeInTheDocument()
    expect(screen.queryByText("Proposal for ACT-002")).not.toBeInTheDocument()
    expect(document.body.textContent).not.toContain("FOREIGN_INCIDENT_CONTENT")
    expect(document.body.textContent).not.toContain("FOREIGN_TARGET_CONTENT")

    await userEvent.click(screen.getByRole("combobox", { name: "Incident action" }))
    await userEvent.click(await screen.findByRole("option", { name: "ACT-002" }))
    expect(screen.getByText("Proposal for ACT-002")).toBeInTheDocument()
    expect(screen.getByText("REASON_ACT-002")).toBeInTheDocument()
    expect(screen.queryByText("Restore the observed healthy template")).not.toBeInTheDocument()
    expect(screen.queryByText("Synthetic execution result")).not.toBeInTheDocument()
    expect(screen.queryByText("BASELINE_HEALTH_STATUS")).not.toBeInTheDocument()
    expect(screen.getByText(/No post-repair verification required/)).toBeInTheDocument()
    expect(screen.getByText(/No execution scheduled: the Guard rejected this proposal/)).toBeInTheDocument()
  })

  it("retains an inspected action across refresh and resets to the next incident's active action", async () => {
    const data = fixture()
    addAction(data)
    const view = details(data)
    await userEvent.click(screen.getByRole("combobox", { name: "Incident action" }))
    await userEvent.click(await screen.findByRole("option", { name: "ACT-002" }))
    const refreshed = structuredClone(data)
    refreshed.generated_at = new Date(Date.now() + 2000).toISOString()
    view.rerender(<IncidentDetails snapshot={refreshed} incidentId="INC-001" open onOpenChange={vi.fn()} />)
    expect(screen.getByText("Proposal for ACT-002")).toBeInTheDocument()
    refreshed.incidents.push({ ...refreshed.incidents[0], incident_id: "INC-NEW", active_action_id: "ACT-NEW" })
    const proposal = { ...refreshed.actions[0].proposal, incident_id: "INC-NEW", action_id: "ACT-NEW", rationale: "New incident proposal" }
    refreshed.actions.push({ action_id: "ACT-NEW", incident_id: "INC-NEW", proposal, state: "VALIDATING", guard_result: null, execution_result: null })
    view.rerender(<IncidentDetails snapshot={refreshed} incidentId="INC-NEW" open onOpenChange={vi.fn()} />)
    expect(screen.getByText("New incident proposal")).toBeInTheDocument()
    expect(screen.queryByText("Proposal for ACT-002")).not.toBeInTheDocument()
  })

  it("shows rejection evidence without borrowing an earlier action's execution or claiming current health", () => {
    const data = fixture()
    addAction(data)
    data.incidents[0].active_action_id = "ACT-002"
    data.incidents[0].state = "REJECTED"
    data.messages.find(record => record.message.kind === "incident")!.message.signals = [{ reason: "InjectedFalseAlarm", source: "demo_harness",
      detail: "Injected false alarm to test a redundant rollback proposal.", at: data.generated_at }]
    const evidence = { sequence: 20, at: data.generated_at, component: 5, event_type: "status", target: data.incidents[0].target,
      incident_id: "INC-001", action_id: "ACT-002", summary: "Executor refused the rejected action: zero patch attempts.", metrics: {} }
    data.telemetry.push(evidence, { ...evidence, sequence: 21, action_id: "ACT-001", summary: "FOREIGN_EXECUTOR_EVIDENCE" })
    details(data)
    expect(screen.getByText("BLOCKED")).toBeInTheDocument()
    expect(screen.getByText("NOT REQUIRED")).toBeInTheDocument()
    expect(screen.getByText(/No execution scheduled: the Guard rejected this proposal/)).toBeInTheDocument()
    expect(screen.getByText(evidence.summary)).toBeInTheDocument()
    expect(screen.queryByText("FOREIGN_EXECUTOR_EVIDENCE")).not.toBeInTheDocument()
    expect(screen.queryByText("Synthetic execution result")).not.toBeInTheDocument()
    expect(screen.queryByText(/No post-execution verification for the selected action/)).not.toBeInTheDocument()
    expect(screen.getByText("False alarm submitted")).toBeInTheDocument()
    expect(screen.queryByText("Failure detected")).not.toBeInTheDocument()
  })

  it("uses database sequence and explicitly labels missing producer timestamps", () => {
    const data = fixture()
    data.messages = [...data.messages].reverse()
    data.messages.find(record => record.message.kind === "execution_result")!.message.recorded_at = "2020-01-01T00:00:00Z"
    details(data)
    expect(screen.getAllByText("No producer timestamp").length).toBeGreaterThan(0)
    const sequence = screen.getByRole("list", { name: "Stored incident event sequence" })
    const events = within(sequence).getAllByRole("listitem")
    expect(events.map(event => event.textContent)).toEqual([
      expect.stringContaining("Failure detected"), expect.stringContaining("Recovery proposed"),
      expect.stringContaining("Dependencies checked"), expect.stringContaining("Guard decision"),
      expect.stringContaining("Execution recorded"), expect.stringContaining("Outcome verified"),
    ])
    expect(screen.getByText(/elapsed recovery time is not inferred/)).toBeInTheDocument()
  })

  it.each(["checked_at", "proposal_sha256", "inputs_sha256"] as const)("does not attach findings with a mismatching %s", field => {
    const data = fixture()
    data.reports[0][field] = "different"
    details(data)
    expect(screen.queryByText("BASELINE_HEALTH_STATUS")).not.toBeInTheDocument()
    expect(screen.getByText(/No matching detailed report/)).toBeInTheDocument()
  })

  it("preserves a historical ALLOW without giving an expired decision a green permission badge", () => {
    const data = fixture()
    data.mode = "recorded/live producers"
    data.actions[0].guard_result!.valid_until = "2020-01-01T00:00:00Z"
    details(data)
    expect(screen.getByText("Expired decision")).toBeInTheDocument()
    expect(screen.getByText("ALLOW · recorded")).not.toHaveClass("text-emerald-600")
    expect(screen.getByText(/cannot authorize a new execution/)).toBeInTheDocument()
    expect(screen.getByText(/not a statement of current workload health/)).toBeInTheDocument()
  })

  it("falls back to the latest proposal sequence and safely explains unavailable records", () => {
    const data = fixture()
    data.incidents[0].active_action_id = null
    addAction(data)
    const view = details(data)
    expect(screen.getByText("Proposal for ACT-002")).toBeInTheDocument()
    expect(screen.getByText(/Fixture clock \/ synthetic/)).toBeInTheDocument()
    const missing = { ...data, incidents: [] }
    view.rerender(<IncidentDetails snapshot={missing} incidentId="INC-001" open onOpenChange={vi.fn()} />)
    expect(screen.getByText(/unavailable in the current bounded snapshot/)).toBeInTheDocument()
    expect(screen.queryByText("Proposal for ACT-002")).not.toBeInTheDocument()
  })
})
