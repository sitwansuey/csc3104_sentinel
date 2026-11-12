import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import { BoutiqueOverview } from "./boutique-overview"
import type { BoutiqueObservation, Cluster } from "@/lib/types"

const now = Date.parse("2026-11-04T01:00:00Z")
const cluster: Cluster = {cluster_id: "connected", namespace: "boutique", observed_at: new Date(now).toISOString(),
  sources: {pods: {status: "available"}, events: {status: "available"}}, nodes: [], pods: [], events: [], summary: {}, truncation: {}}
function observations(): BoutiqueObservation[] {
  return [{ sequence: 2, cluster_id: "connected", namespace: "boutique", observed_at: new Date(now).toISOString(),
    stage: "inventory", incident_id: null, action_id: null, journey: null, services: [
      {name: "shippingservice", desired: 1, ready: 0, available: 0, generation: 2, observed_generation: 2, ready_state: false, recovery_target: true},
      {name: "frontend", desired: 1, ready: 1, available: 1, generation: 1, observed_generation: 1, ready_state: true, recovery_target: false},
    ] }, { sequence: 1, cluster_id: "connected", namespace: "boutique", observed_at: new Date(now).toISOString(),
      stage: "primary-fault", incident_id: "INC-1", action_id: null, services: null,
      journey: {home_status: 200, add_to_cart_status: 302, cart_status: 500, checkout_status: 500, passed: false, order_confirmed: false} }]
}

describe("Boutique application overview", () => {
  it("shows downstream failure despite frontend readiness, and opens the matching incident", async () => {
    const inspect = vi.fn()
    render(<BoutiqueOverview records={observations()} cluster={cluster} now={now} stale={false} inspect={inspect}/>)
    expect(within(screen.getByRole("article", {name: "frontend readiness"})).getByText("READY")).toBeInTheDocument()
    expect(within(screen.getByRole("article", {name: "shippingservice readiness"})).getByText("NotReady")).toBeInTheDocument()
    expect(screen.getAllByText("500")).toHaveLength(2)
    expect(screen.getByText("FAIL")).toBeInTheDocument()
    expect(screen.queryByText("RECOVERED")).not.toBeInTheDocument()
    expect(screen.getByText("Pod inventory")).toBeInTheDocument()
    expect(screen.getByText("Kubernetes warning events")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", {name: "INC-1"}))
    expect(inspect).toHaveBeenCalledWith("INC-1")
  })

  it.each([{stale: true, elapsed: 0}, {stale: false, elapsed: 16000}, {stale: false, elapsed: -1000}])(
    "never presents stale, disconnected or future observations as current readiness: %j", ({stale, elapsed}) => {
      render(<BoutiqueOverview records={observations()} cluster={cluster} now={now+elapsed} stale={stale} inspect={vi.fn()}/>)
      expect(screen.getByText("Last known READY")).toBeInTheDocument()
      expect(screen.queryByText("READY", {exact:true})).not.toBeInTheDocument()
    })

  it("names the run's recovery target from the inventory", () => {
    const records = observations()
    records[0].services = records[0].services!.map(service =>
      ({...service, name: service.name === "shippingservice" ? "cartservice" : service.name}))
    render(<BoutiqueOverview records={records} cluster={cluster} now={now} stale={false} inspect={vi.fn()}/>)
    expect(screen.getByText(/restricted recovery in this run target cartservice\./)).toBeInTheDocument()
    expect(screen.queryByText(/shipping only/)).not.toBeInTheDocument()
    expect(screen.getByText("Target fault")).toBeInTheDocument()
  })

  it("does not mix scopes or turn absent data into success", () => {
    render(<BoutiqueOverview records={observations()} cluster={{...cluster, namespace:"other"}} now={now} stale={false} inspect={vi.fn()}/>)
    expect(screen.getByText("No Boutique inventory yet")).toBeInTheDocument()
    expect(screen.getByText("No shopping requests measured yet")).toBeInTheDocument()
    expect(screen.queryByText("500")).not.toBeInTheDocument()
  })

  it("keeps unknown request results separate from a passed journey and labels playback", () => {
    const records = observations()
    records[1].journey!.passed = null
    records[1].journey!.checkout_status = null
    render(<BoutiqueOverview records={records} cluster={cluster} now={now} stale={false} playback inspect={vi.fn()}/>)
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument()
    expect(screen.getByText("Unknown")).toBeInTheDocument()
    expect(screen.getByText("Playback READY")).toBeInTheDocument()
    expect(screen.queryByText("PASS")).not.toBeInTheDocument()
  })
})
