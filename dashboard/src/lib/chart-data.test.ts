import { describe, expect, it } from "vitest"
import { fixture } from "@/test/fixtures"
import type { Cluster, Telemetry } from "./types"
import { fullTimestamp, readinessLabel, readinessSeries, resourceSeries, timeTick, timeTicks, workloadFaultMarkers } from "./chart-data"

const now=Date.parse("2026-10-25T10:00:00.000Z")
function cluster(at:number, extra:Partial<Cluster>={}):Cluster {
  return {...fixture().cluster_history[0],observed_at:new Date(at).toISOString(),...extra}
}
function fault(at:number, extra:Partial<Telemetry>={}):Telemetry {
  return {sequence:1,at:new Date(at).toISOString(),component:1,event_type:"fault",summary:"Fault recorded",metrics:{},target:fixture().incidents[0].target,...extra}
}

describe("resource chart timeline",()=>{
  it("anchors live ranges to now and never presents old samples as recent",()=>{
    const history=[cluster(now-600_000),cluster(now-590_000)]
    expect(resourceSeries(history,[],"5m",false,now).data).toEqual([])
    const demo=resourceSeries(history,[],"5m",true,now)
    expect(demo.data).toHaveLength(2)
    expect(demo.anchor).toBe(now-590_000)
    expect(demo.limitedCoverage).toBe(true)
  })
  it("sorts by full producer timestamps across midnight and preserves missing usage",()=>{
    const midnight=Date.parse("2026-10-25T00:00:00Z")
    const missing=cluster(midnight)
    missing.summary={...missing.summary,cpu_usage_cores:null,memory_allocatable_bytes:0}
    const series=resourceSeries([cluster(midnight+10_000),cluster(midnight-10_000),missing],[],"all",true,now)
    expect(series.data.map(point=>point.timestamp)).toEqual([midnight-10_000,midnight,midnight+10_000])
    expect(series.data[0].cpu).toBe(25)
    expect(series.data[1].cpu).toBeNull()
    expect(series.data[1].memory).toBeNull()
    expect(series.domain).toEqual([midnight-10_000,midnight+10_000])
  })
  it("plots only exact cluster and namespace scoped faults within the visible sample coverage",()=>{
    const target=fixture().incidents[0].target
    const markers=[fault(now-20_000),fault(now-20_000,{sequence:2}),fault(now-10_000,{target:null}),fault(now-10_000,{target:{...target,cluster_id:"other"}}),fault(now-10_000,{target:{...target,namespace:"other"}}),fault(now-40_000),fault(now-10_000,{event_type:"error"})]
    const series=resourceSeries([cluster(now-30_000),cluster(now)],markers,"5m",false,now)
    expect(series.markers).toHaveLength(1)
    expect(series.markers[0].timestamp).toBe(now-20_000)
    expect(series.markers[0].events).toHaveLength(2)
    expect(series.unscopedMarkers).toBe(1)
    expect(series.outsideMarkers).toBe(1)
  })
  it("keeps marker timestamps stable over polling, range changes and duplicate producer samples",()=>{
    const event=fault(now-60_000)
    const history=[cluster(now),cluster(now-120_000),cluster(now-120_000)]
    const initial=resourceSeries(history,[event],"5m",false,now)
    const next=resourceSeries([cluster(now+2_000),...history],[event],"15m",false,now+2_000)
    expect(initial.markers[0].timestamp).toBe(next.markers[0].timestamp)
    expect(initial.data).toHaveLength(2)
    expect(initial.limitedCoverage).toBe(true)
    expect(initial.data.some(point=>point.timestamp===now-300_000)).toBe(false)
  })
  it("excludes invalid and future samples rather than moving the live anchor",()=>{
    const series=resourceSeries([cluster(now+30_000),cluster(now-1_000),cluster(now,{observed_at:"invalid"})],[],"5m",false,now)
    expect(series.anchor).toBe(now)
    expect(series.data.map(point=>point.timestamp)).toEqual([now-1_000])
  })
})

describe("readiness chart and freshness",()=>{
  it("uses actual producer dates for ranges and retains zero replicas",()=>{
    const points=[{time:"09:00",capturedAt:new Date(now-600_000).toISOString(),ready:1,desired:1},{time:"10:00",capturedAt:new Date(now).toISOString(),ready:0,desired:1}]
    const series=readinessSeries(points,"5m",false,now)
    expect(series.timed).toBe(true)
    expect(series.data).toHaveLength(1)
    expect(series.data[0].ready).toBe(0)
    expect(readinessSeries(points,"5m",true,now+600_000).data).toHaveLength(1)
  })
  it("does not fabricate dates from legacy time-of-day labels",()=>{
    const points=[{time:"23:59",ready:0,desired:1},{time:"00:01",ready:1,desired:1}]
    const series=readinessSeries(points,"5m",false,now)
    expect(series.timed).toBe(false)
    expect(series.data).toEqual(points)
    expect(series.anchor).toBeNull()
  })
  it("distinguishes current, stale, disconnected, simulated and unknown readiness",()=>{
    const recent=new Date(now-1_000).toISOString(),old=new Date(now-15_001).toISOString()
    expect(readinessLabel(true,recent,false,false,now)).toBe("Ready")
    expect(readinessLabel(false,recent,false,false,now)).toBe("NotReady")
    expect(readinessLabel(true,old,false,false,now)).toBe("Last known Ready")
    expect(readinessLabel(true,recent,true,false,now)).toBe("Last known Ready")
    expect(readinessLabel(true,recent,false,true,now)).toBe("SIMULATED Ready")
    expect(readinessLabel(true,undefined,false,false,now)).toBe("Readiness unknown")
    expect(readinessLabel(null,recent,false,false,now)).toBe("Readiness unknown")
    expect(fullTimestamp(now)).toContain("2026-10-25T10:00:00.000Z")
  })
})

describe("workload fault markers",()=>{
  it("plots only faults for the exact workload inside the plotted coverage",()=>{
    const target=fixture().incidents[0].target
    const markers=workloadFaultMarkers([fault(now-10_000),fault(now-10_000,{sequence:2}),fault(now-10_000,{target:{...target,name:"other"}}),fault(now-10_000,{target:null}),fault(now-90_000),fault(now-5_000,{event_type:"error"})],target,[now-60_000,now])
    expect(markers).toHaveLength(1)
    expect(markers[0].events).toHaveLength(2)
    expect(workloadFaultMarkers([fault(now)],null,[now-1,now+1])).toEqual([])
  })
})

describe("axis tick labels",()=>{
  it("keeps labels distinct across sub-10-second spans",()=>{
    const short=timeTicks([now,now+900])
    expect(short(now)).not.toBe(short(now+500))
    expect(timeTicks([now,now+60_000])).toBe(timeTick)
    expect(timeTicks(null)).toBe(timeTick)
  })
})
