import { describe, expect, it } from "vitest"
import { observedClusters, scope } from "./data"
import { fixture } from "@/test/fixtures"

describe("explicit observed cluster selection",()=>{
  it("filters incident, workload and metric records consistently",()=>{
    const data=fixture(),other=structuredClone(data.incidents[0])
    other.incident_id="INC-OTHER";other.target.cluster_id="other-cluster"
    data.incidents.push(other)
    const metrics=structuredClone(data.cluster_history[0]);metrics.cluster_id="other-cluster"
    data.cluster_history.unshift(metrics)
    const scoped=scope(data,"","","sentinel-lab")
    expect(scoped.incidents.map(i=>i.incident_id)).toEqual(["INC-001"])
    expect(scoped.clusters.every(c=>c.cluster_id==="sentinel-lab")).toBe(true)
    expect(scope(data,"INC-OTHER","","sentinel-lab").messages).toHaveLength(0)
    expect(scope(data,"","","unknown-cluster").clusters).toHaveLength(0)
  })
  it("describes last observed telemetry rather than inventing connection health",()=>{
    const data=fixture();data.mode="recorded/live producers"
    const at=new Date(data.cluster_history[0].observed_at).getTime()
    expect(observedClusters(data,at)[0].status).toBe("Receiving")
    expect(observedClusters(data,at+16000)[0].status).toBe("Stale")
    data.cluster_history.forEach(c=>{c.sources.nodes={status:"unavailable"};c.sources.pods={status:"unavailable"}})
    expect(observedClusters(data,at)[0].status).toBe("Unavailable")
    data.cluster_history=[]
    expect(observedClusters(data,at)[0].status).toBe("No sample")
  })
  it("retains unscoped system markers without leaking known other-cluster markers",()=>{
    const data=fixture()
    data.telemetry.push({...data.telemetry[1],sequence:3,target:{...data.incidents[0].target,cluster_id:"other"},summary:"Other cluster marker"})
    const scoped=scope(data,"","","sentinel-lab")
    expect(scoped.telemetry.some(e=>e.target===null)).toBe(true)
    expect(scoped.telemetry.some(e=>e.summary==="Other cluster marker")).toBe(false)
  })
})
