import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import { SecurityOverview } from "./security-overview"
import type { SecurityEvent } from "@/lib/types"

describe("security evidence",()=>{
  it("keeps absent evidence unknown",()=>{
    render(<SecurityOverview events={[]}/>)
    expect(screen.getByText(/No images scanned/)).toBeInTheDocument()
    expect(screen.getByText(/No inspection evidence/)).toBeInTheDocument()
    expect(screen.queryByText("NO_KNOWN_CVES")).not.toBeInTheDocument()
  })
  it("a failed latest scan replaces older clean status",()=>{
    render(<SecurityOverview events={[
      {sequence:2,kind:"image_scan",at:"2026-11-04T01:00:00Z",image:"app@sha256:abc",status:"UNKNOWN",counts:{},findings:[]},
      {sequence:1,kind:"image_scan",at:"2026-11-03T01:00:00Z",image:"app@sha256:abc",status:"NO_KNOWN_CVES",counts:{HIGH:0,CRITICAL:0},findings:[]},
    ]}/>)
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument()
    expect(screen.queryByText("NO_KNOWN_CVES")).not.toBeInTheDocument()
    expect(screen.getByText(/Scan unavailable/)).toBeInTheDocument()
  })
  it("shows package fixes separately from inspection decisions without patch buttons",()=>{
    const events:SecurityEvent[]=[{sequence:2,kind:"image_scan",at:"2026-11-04T01:00:00Z",image:"app@sha256:abc",status:"FINDINGS",counts:{HIGH:1,CRITICAL:0},
      findings:[{id:"CVE-TEST-1",package:"openssl",installed:"1.0",fixed:"1.1",severity:"HIGH",recommendation:"Rebuild and test."}]},
      {sequence:1,kind:"inspection",at:"2026-11-04T01:00:00Z",method:"/shipping/GetQuote",verdict:"DENY",rule:"QUANTITY",bytes:20}]
    render(<SecurityOverview events={events}/>)
    expect(screen.getByText("CVE-TEST-1")).toBeInTheDocument()
    expect(screen.getByText("1.1")).toBeInTheDocument()
    expect(screen.getByText("QUANTITY")).toBeInTheDocument()
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
  })
  it("marks an old or future monitor heartbeat stale",()=>{
    const now=Date.parse("2026-11-04T01:00:00Z")
    render(<SecurityOverview now={now} events={[{sequence:1,kind:"monitor",at:"2026-11-04T00:59:00Z",status:"WAITING",completed:13,total:13,interval_seconds:86400}]}/>)
    expect(screen.getByText("STALE")).toBeInTheDocument()
    expect(screen.getByText(/cannot be assumed active/)).toBeInTheDocument()
  })
  it("shows scheduled progress and explicit open or resolved alerts",()=>{
    const now=Date.parse("2026-11-04T01:00:00Z")
    render(<SecurityOverview now={now} events={[
      {sequence:2,kind:"monitor",at:new Date(now).toISOString(),status:"SCANNING",completed:2,total:13,interval_seconds:86400},
      {sequence:1,kind:"alert",at:new Date(now).toISOString(),code:"CVE_SCAN_FAILED",severity:"HIGH",status:"OPEN",detail:"Scan unavailable."}
    ]}/>)
    expect(screen.getByText("SCANNING")).toBeInTheDocument()
    expect(screen.getByText(/2 \/ 13 images/)).toBeInTheDocument()
    expect(screen.getByText("CVE_SCAN_FAILED")).toBeInTheDocument()
    expect(screen.getByText("OPEN")).toBeInTheDocument()
  })
})
