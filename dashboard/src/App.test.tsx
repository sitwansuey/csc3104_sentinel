import { act, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import App from "./App"
import { fixture } from "./test/fixtures"
import { bytes, percent, scope, identity } from "./lib/data"
import type { Snapshot } from "./lib/types"

let data:Snapshot
let fetchMock:ReturnType<typeof vi.fn>
beforeEach(()=>{
  data=fixture()
  fetchMock=vi.fn().mockImplementation(async()=>({ok:true,json:async()=>structuredClone(data)}))
  vi.stubGlobal("fetch",fetchMock)
})
async function ready(){render(<App/>);await screen.findByText("Explore the recovery loop.")}
async function tab(name:string){await userEvent.click(screen.getByRole("tab",{name}));return screen.getByRole("tabpanel")}

describe("shadcn dashboard",()=>{
  it("renders actual shadcn tabs, cards, selectors and all eight feeds",async()=>{
    await ready()
    expect(screen.getAllByRole("tab")).toHaveLength(8)
    expect(document.querySelector('[data-slot="card"]')).toBeInTheDocument()
    expect(screen.getByRole("combobox",{name:"Incident filter"})).toHaveAttribute("data-slot","select-trigger")
    const expectations:[string,string][]=[["Kubernetes","Pod inventory"],["Coordinator","Recorded approvals"],["Monitoring","Detection evidence"],["Planner / RCA","Restore the observed healthy template"],["Recovery Guard","BASELINE_HEALTH_STATUS"],["Executor","Recorded execution attempts"],["Dependencies / Health","Healthy samples over the required window"]]
    for(const [name,text] of expectations){const panel=await tab(name);expect(within(panel).getAllByText(text).length).toBeGreaterThan(0)}
    expect(document.body.textContent).not.toContain("undefined")
  })
  it("shows real units and explicit unavailable metrics instead of fabricated zero",async()=>{
    data.mode="recorded/live producers"
    data.cluster_history[0].summary.cpu_usage_cores=null
    data.cluster_history[0].summary.memory_usage_bytes=null
    data.cluster_history[0].sources.node_metrics={status:"unavailable",error_code:"API_UNAVAILABLE"}
    data.cluster_history[0].sources.pod_metrics={status:"unavailable",error_code:"PERMISSION_DENIED"}
    data.cluster_history=data.cluster_history.slice(0,1)
    render(<App/>);await screen.findByText("Every signal. In context.")
    const panel=await tab("Kubernetes")
    expect(within(panel).getByText("Usage metrics unavailable")).toBeInTheDocument()
    expect(within(panel).getByText("Read permission denied")).toBeInTheDocument()
    expect(within(panel).getByText("125m")).toBeInTheDocument()
    expect(within(panel).getByText("64 MiB")).toBeInTheDocument()
    expect(document.body.textContent).not.toContain("NaN")
  })
  it("keeps filtering controlled and binds report findings to the selected action",async()=>{
    await ready()
    await userEvent.click(screen.getByRole("combobox",{name:"Incident filter"}))
    await userEvent.click(await screen.findByRole("option",{name:"INC-001"}))
    const panel=await tab("Recovery Guard")
    expect(within(panel).getByText("BASELINE_HEALTH_STATUS")).toBeInTheDocument()
    data.reports[0].inputs_sha256="mismatched"
    await userEvent.click(screen.getByRole("button",{name:"Refresh dashboard"}))
    await waitFor(()=>expect(within(screen.getByRole("tabpanel")).queryByText("BASELINE_HEALTH_STATUS")).not.toBeInTheDocument())
    expect(screen.getByText(/No matching detailed report/)).toBeInTheDocument()
  })
  it("pauses automatic polling while retaining manual refresh",async()=>{
    vi.useFakeTimers()
    render(<App/>)
    await act(async()=>{await Promise.resolve()})
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await act(async()=>{await vi.advanceTimersByTimeAsync(2000)})
    expect(fetchMock).toHaveBeenCalledTimes(2)
    act(()=>screen.getByRole("button",{name:"Pause updates"}).click())
    await act(async()=>{await vi.advanceTimersByTimeAsync(4000)})
    expect(fetchMock).toHaveBeenCalledTimes(2)
    act(()=>screen.getByRole("button",{name:"Refresh dashboard"}).click())
    await act(async()=>{await Promise.resolve()})
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(screen.getByText("Updates paused")).toBeInTheDocument()
  })
  it("supports tab keyboard navigation and text-safe producer content",async()=>{
    data.messages.find(r=>r.message.kind==="proposal")!.message.rationale="<img src=x onerror=alert(1)>"
    await ready()
    const planner=screen.getByRole("tab",{name:"Planner / RCA"})
    await userEvent.click(planner);await userEvent.keyboard("{ArrowDown}")
    expect(screen.getByRole("tab",{name:"Recovery Guard"})).toHaveAttribute("aria-selected","true")
    await tab("Planner / RCA")
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument()
    expect(document.querySelector("img")).toBeNull()
  })
  it("retains a disconnection warning across navigation and recovers on refresh",async()=>{
    await ready()
    fetchMock.mockRejectedValue(new Error("offline"))
    await userEvent.click(screen.getByRole("button",{name:"Refresh dashboard"}))
    await screen.findByText(/Dashboard connection lost/)
    await tab("Kubernetes")
    expect(screen.getByText(/Dashboard connection lost/)).toBeInTheDocument()
    fetchMock.mockImplementation(async()=>({ok:true,json:async()=>structuredClone(data)}))
    await userEvent.click(screen.getByRole("button",{name:"Refresh dashboard"}))
    await waitFor(()=>expect(screen.queryByText(/Dashboard connection lost/)).not.toBeInTheDocument())
  })
})
describe("dashboard review improvements",()=>{
  it("selects the observed cluster explicitly and shows its connection status",async()=>{
    data.mode="recorded/live producers"
    render(<App/>);await screen.findByText("Every signal. In context.")
    await waitFor(()=>expect(screen.getByRole("combobox",{name:"Cluster filter"})).toHaveTextContent("sentinel-lab"))
    expect(screen.getByLabelText("Cluster connection: Receiving")).toBeInTheDocument()
  })
  it("opens one incident in a combined detail drawer from the register",async()=>{
    await ready()
    await userEvent.click(screen.getByRole("button",{name:"Open incident INC-001"}))
    const drawer=await screen.findByRole("dialog",{name:"INC-001"})
    for(const text of ["Detection evidence","Recovery proposal","Guard evaluation","Execution","Outcome after execution"])expect(within(drawer).getByText(text)).toBeInTheDocument()
  })
  it("never shows an expired ALLOW or a disconnected observation as current",async()=>{
    data.mode="recorded/live producers"
    data.messages.find(r=>r.message.kind==="guard_result")!.message.valid_until=new Date(Date.now()-60_000).toISOString()
    data.messages.find(r=>r.message.kind==="guard_result")!.message.checked_at=new Date(Date.now()-120_000).toISOString()
    render(<App/>);await screen.findByText("Every signal. In context.")
    const panel=await tab("Recovery Guard")
    expect(within(panel).getAllByText("ALLOW · EXPIRED")[0]).not.toHaveClass("text-emerald-600")
    fetchMock.mockRejectedValue(new Error("offline"))
    await userEvent.click(screen.getByRole("button",{name:"Refresh dashboard"}))
    await screen.findByText(/Dashboard connection lost/)
    await tab("Monitoring")
    expect(within(screen.getByRole("tabpanel")).getAllByText("STALE").length).toBeGreaterThan(0)
    expect(screen.getByLabelText("Cluster connection: Stale")).toBeInTheDocument()
  })
  it("changes the chart time range from one shared control",async()=>{
    await ready()
    const range=screen.getByRole("group",{name:"Chart time range"})
    expect(within(range).getByRole("button",{name:"15m"})).toHaveAttribute("aria-pressed","true")
    await userEvent.click(within(range).getByRole("button",{name:"5m"}))
    expect(within(range).getByRole("button",{name:"5m"})).toHaveAttribute("aria-pressed","true")
    expect(screen.getByText(/Last 5m/)).toBeInTheDocument()
  })
})
describe("metrics and scope",()=>{
  it("preserves unknown values and exact target filtering",()=>{
    expect(percent(null,4)).toBeNull();expect(percent(1,0)).toBeNull();expect(percent(1,4)).toBe(25)
    expect(bytes(null)).toBe("—");expect(bytes(1024**3)).toBe("1 GiB")
    const scoped=scope(data,"INC-001",identity(data.incidents[0].target))
    expect(scoped.clusters).toHaveLength(3);expect(scoped.messages).toHaveLength(6)
    expect(scope(data,"INC-001","different|namespace|Deployment|other|uid").messages).toHaveLength(0)
  })
})
