import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { LabExperiment } from "./lab-experiment"
import type { LabSession } from "@/lib/types"

function session(): LabSession {
  return {session_id: "session-1", state: "READY", gate_id: "gate-1", active_scenario: "primary",
    can_inject: true, updated_at: "2026-11-04T00:00:00Z", planning: "Harness-authored proposals; no model call",
    target: {cluster_id: "sentinel-boutique-lab", namespace: "sentinel-boutique", name: "shippingservice"},
    scenarios: [{id: "primary", title: "Bad rollout", description: "Inject an invalid PORT, then run guarded recovery.",
      expected: "RECOVERED", status: "READY", requested_at: null, finished_at: null, incident_id: null, action_id: null}]}
}
const refresh = vi.fn(), inspect = vi.fn()
function show(lab = session(), stale = false, paused = false) {
  return render(<LabExperiment lab={lab} stale={stale} paused={paused} now={Date.parse(lab.updated_at)} refresh={refresh} inspect={inspect}/>)
}
beforeEach(() => {vi.clearAllMocks()})

describe("opt-in Boutique lab", () => {
  it("submits the current gate using local capability and never chooses a target or action", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce({ok: true, json: async () => ({session_id: "session-1", control_token: "secret"})})
      .mockResolvedValueOnce({ok: true, status: 202})
    vi.stubGlobal("fetch", fetcher)
    show()
    await userEvent.click(screen.getByRole("button", {name: "Inject invalid PORT"}))
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce())
    const [path, options] = fetcher.mock.calls[1]
    expect(path).toBe("/api/lab/inject")
    expect(JSON.parse(options.body)).toEqual({session_id: "session-1", gate_id: "gate-1"})
    expect(options.headers["X-Sentinel-Lab-Token"]).toBe("secret")
    expect(screen.getByRole("status")).toHaveTextContent("Injection requested")
    expect(screen.getByText(/Harness-authored proposals/)).toBeInTheDocument()
  })
  it("disables injection during setup, execution, disconnected or paused updates", () => {
    const view = show({...session(), state: "PREPARING", gate_id: null, active_scenario: null, can_inject: false})
    expect(screen.getByRole("button")).toBeDisabled()
    view.rerender(<LabExperiment lab={{...session(), state: "RUNNING", can_inject: false}} stale={false} paused={false} now={0} refresh={refresh} inspect={inspect}/>)
    expect(screen.getByRole("button", {name: "Scenario running"})).toBeDisabled()
    view.rerender(<LabExperiment lab={session()} stale={true} paused={false} now={0} refresh={refresh} inspect={inspect}/>)
    expect(screen.getByRole("button", {name: "Inject invalid PORT"})).toBeDisabled()
    view.rerender(<LabExperiment lab={session()} stale={false} paused={true} now={0} refresh={refresh} inspect={inspect}/>)
    expect(screen.getByRole("button", {name: "Inject invalid PORT"})).toBeDisabled()
  })
  it("refuses to post when the backend has moved to another session", async () => {
    const fetcher = vi.fn().mockResolvedValue({ok: true, json: async () => ({session_id: "other", control_token: "secret"})})
    vi.stubGlobal("fetch", fetcher)
    show()
    await userEvent.click(screen.getByRole("button", {name: "Inject invalid PORT"}))
    expect(await screen.findByRole("alert")).toHaveTextContent("session changed")
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(refresh).not.toHaveBeenCalled()
  })
  it("reports a stale-gate refusal without displaying a successful injection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({ok: true, json: async () => ({session_id: "session-1", control_token: "secret"})})
      .mockResolvedValueOnce({ok: false, status: 409}))
    show()
    await userEvent.click(screen.getByRole("button", {name: "Inject invalid PORT"}))
    expect(await screen.findByRole("alert")).toHaveTextContent("scenario changed")
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
  })
  it("shows measured terminal results and opens the recorded incident", async () => {
    const lab = session()
    lab.state = "COMPLETED"; lab.can_inject = false; lab.gate_id = null
    lab.scenarios[0] = {...lab.scenarios[0], status: "RECOVERED", requested_at: "2026-11-04T00:00:00Z",
      finished_at: "2026-11-04T00:00:12Z", incident_id: "INC-1", action_id: "ACT-1"}
    show(lab)
    expect(screen.getByText("All scenarios reached their expected outcomes")).toBeInTheDocument()
    expect(screen.getByText("12s since request to outcome")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", {name: "Inspect incident"}))
    expect(inspect).toHaveBeenCalledWith("INC-1")
  })
})
