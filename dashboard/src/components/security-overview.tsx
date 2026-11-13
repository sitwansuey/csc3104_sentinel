import { Context, Panel, Prose, Records, Status } from "@/components/records"
import { time } from "@/lib/data"
import type { SecurityEvent } from "@/lib/types"

export function SecurityOverview({events,now=Date.now()}:{events:SecurityEvent[];now?:number}) {
  const scans = new Map<string,SecurityEvent>()
  events.filter(e=>e.kind==="image_scan").forEach(e=>{if(e.image&&!scans.has(e.image))scans.set(e.image,e)})
  const inspections = events.filter(e=>e.kind==="inspection")
  const alerts = events.filter(e=>e.kind==="alert")
  const monitor = events.find(e=>e.kind==="monitor")
  const monitorFresh = monitor && now-new Date(monitor.at).getTime()>=0 && now-new Date(monitor.at).getTime()<=45000
  return <>
    <Context>Security evidence from this connected journal, across its image scans and proxy traffic. Results are historical measurements. CVE recommendations do not change images or approve recovery.</Context>
    <Panel title="Security monitoring" description="Local scheduled scanner and inspection alerts; keep the monitor process running">
      <div className="space-y-2 p-5 text-sm">
        <Status value={!monitor?"NOT CONNECTED":!monitorFresh?"STALE":monitor.status||"UNKNOWN"}/>
        {monitor&&<><p>Scan progress: {monitor.completed??0} / {monitor.total??0} images</p><p>Interval: {(monitor.interval_seconds||0)/3600} hours · Next scan: {monitor.next_scan_at?time(monitor.next_scan_at):"Not scheduled yet"}</p></>}
        {!monitorFresh&&<p className="text-muted-foreground">No recent monitor heartbeat. Scheduled checks cannot be assumed active.</p>}
      </div>
    </Panel>
    <Panel title="Security alerts" description="Recorded open and resolved transitions; repeated identical alerts are suppressed">
      {alerts.length?<Records headers={["AT","STATE","SEVERITY","REASON","DETAIL"]} rows={alerts.map(e=>[
        time(e.at),<Status value={e.status||"UNKNOWN"}/>,<Status value={e.severity||"UNKNOWN"}/>,e.code,<div><Prose>{e.detail}</Prose>{e.image&&<code className="break-all text-[10px]">{e.image}</code>}</div>
      ])}/>:<p className="p-5 text-sm text-muted-foreground">No alert transitions recorded. This does not establish that the system is threat-free.</p>}
    </Panel>
    <Panel title="Image vulnerability scans" description="Latest scan per immutable image; scanner failures remain unknown">
      {scans.size?<Records headers={["IMAGE / SCANNED AT","RESULT","HIGH / CRITICAL","RECOMMENDATIONS"]} rows={[...scans.values()].map(scan=>[
        <div className="max-w-xl space-y-2"><code className="break-all text-[10px]">{scan.image}</code><p>{time(scan.scanned_at||scan.at)}</p></div>,
        <Status value={scan.status||"UNKNOWN"}/>, scan.status==="UNKNOWN"?"Unknown":`${scan.counts?.HIGH??0} / ${scan.counts?.CRITICAL??0}`,
        <Prose>{scan.status==="UNKNOWN"?"Scan unavailable. Retry before assessing this image.":scan.status==="NO_KNOWN_CVES"?"No known CVEs detected at scan time. Rescan before release.":`${scan.findings?.length||0} package findings. Rebuild, test and rescan before approving a replacement digest.`}</Prose>
      ])}/>:<p className="p-5 text-sm text-muted-foreground">No images scanned. Vulnerability status is unknown.</p>}
    </Panel>
    {[...scans.values()].filter(scan=>scan.findings?.length).map(scan=><Panel key={scan.image} title="Package patch recommendations" description={scan.image}>
      <Records headers={["VULNERABILITY","SEVERITY","PACKAGE","INSTALLED","VENDOR FIX","NEXT STEP"]} rows={(scan.findings||[]).map(f=>[
        f.id,<Status value={f.severity}/>,f.package,f.installed,f.fixed||"No fix listed",<Prose>{f.recommendation}</Prose>
      ])}/>
    </Panel>)}
    <Panel title="gRPC request inspection" description="Envoy decrypts TLS; the inspector validates unary shipping and cart Protobuf requests. Raw payloads, addresses and user ids are excluded.">
      {inspections.length?<Records headers={["AT","RPC","DECISION","RULE","BYTES"]} rows={inspections.map(e=>[
        time(e.at),<code className="break-all text-[10px]">{e.method}</code>,<Status value={e.verdict||"UNKNOWN"}/>,e.rule,e.bytes
      ])}/>:<p className="p-5 text-sm text-muted-foreground">No inspection evidence. Start the security lab and send traffic through its proxy.</p>}
    </Panel>
  </>
}
