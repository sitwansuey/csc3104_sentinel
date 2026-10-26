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

describe("focused recovery Overview", () => {
  it("shows real outcome checks and keeps resource usage off the Overview", () => {
    show(fixture())
    expect(screen.getByText("Application response and replicas")).toBeInTheDocument()
    expect(screen.getByText("1 / 1 checks passed")).toBeInTheDocument()
    expect(screen.getByText("Deployment readiness")).toBeInTheDocument()
    expect(screen.queryByText("Resource utilization")).not.toBeInTheDocument()
    expect(screen.queryByText("CPU usage")).not.toBeInTheDocument()
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
