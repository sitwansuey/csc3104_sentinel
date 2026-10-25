import type { Snapshot, Target } from "./types"

export const identity = (target?: Target | null) => target ? [target.cluster_id, target.namespace, target.kind, target.name, target.uid].join("|") : ""
export const targetLabel = (target?: Target | null) => target ? `${target.namespace} / ${target.name}` : "System / unscoped"
export const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value)
export const format = (value: unknown, digits=1) => isNumber(value) ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : "—"
export const percent = (used: unknown, capacity: unknown) => isNumber(used) && isNumber(capacity) && capacity > 0 ? used / capacity * 100 : null
export function bytes(value: unknown) {
  if (!isNumber(value)) return "—"
  const units = ["B", "KiB", "MiB", "GiB", "TiB"]
  let n=value, index=0
  while(n >= 1024 && index < units.length-1) { n/=1024; index++ }
  return `${format(n)} ${units[index]}`
}
export function time(value?: string | null) {
  if (!value) return "No producer timestamp"
  const date=new Date(value)
  return Number.isNaN(+date) ? "Unknown time" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
}
export function age(value?: string | null) {
  if (!value) return "Unknown age"
  const seconds=Math.floor((Date.now()-new Date(value).getTime())/1000)
  if(!Number.isFinite(seconds)) return "Unknown age"
  if(seconds < 0) return "Future timestamp"
  if(seconds < 60) return `${seconds}s ago`
  if(seconds < 3600) return `${Math.floor(seconds/60)}m ago`
  return `${Math.floor(seconds/3600)}h ago`
}
export function fresh(value?: string | null) { const elapsed=Date.now()-new Date(value || "").getTime(); return elapsed>=0 && elapsed<=15000 }
export function observedClusters(snapshot: Snapshot, now=Date.now()) {
  const names=new Set<string>()
  snapshot.cluster_history?.forEach(c=>names.add(c.cluster_id))
  snapshot.incidents.forEach(i=>names.add(i.target.cluster_id))
  snapshot.telemetry.forEach(e=>{if(e.target)names.add(e.target.cluster_id)})
  return [...names].sort().map(id=>{
    const record=snapshot.cluster_history?.find(c=>c.cluster_id===id)
    const elapsed=record?now-new Date(record.observed_at).getTime():NaN
    const readable=record&&["nodes","pods"].some(source=>record.sources[source]?.status==="available")
    const status=snapshot.mode==="synthetic"?"Demo":!record?"No sample":!Number.isFinite(elapsed)||elapsed<0?"Unknown time":elapsed>15000?"Stale":readable?"Receiving":"Unavailable"
    return {id,status,observedAt:record?.observed_at,label:`${id} · ${status}`}
  })
}
export function scope(snapshot: Snapshot, incidentId: string, workload: string, selectedCluster="") {
  const target=snapshot.incidents.find(i=>i.incident_id===incidentId)?.target
  const clusterId=selectedCluster || workload.split("|")[0] || target?.cluster_id
  const matches=(item: { incident_id?: string; target?: Target | null })=>(!clusterId || item.target?.cluster_id===clusterId) && (!incidentId || item.incident_id===incidentId) && (!workload || identity(item.target)===workload)
  const actions=snapshot.actions.filter(a=>matches({ ...a, target:a.proposal.target }))
  const telemetryTarget=workload || (incidentId ? identity(target) : "")
  const namespace=workload.split("|")[1] || target?.namespace
  const history=(snapshot.cluster_history || []).filter(c=>(!clusterId || c.cluster_id===clusterId) && (!namespace || c.namespace===namespace))
  const latest=history[0]
  return {
    incidents:snapshot.incidents.filter(matches), actions, actionIds:new Set(actions.map(a=>a.action_id)),
    messages:snapshot.messages.filter(r=>matches(r.message)),
    telemetry:snapshot.telemetry.filter(e=>{
      const associated=e.target || snapshot.incidents.find(i=>i.incident_id===e.incident_id)?.target || snapshot.actions.find(a=>a.action_id===e.action_id)?.proposal.target
      return (!clusterId || !associated || associated.cluster_id===clusterId) && (!telemetryTarget || !associated || identity(associated)===telemetryTarget) && (!incidentId || !e.incident_id || e.incident_id===incidentId)
    }),
    clusters:latest ? history.filter(c=>c.cluster_id===latest.cluster_id && c.namespace===latest.namespace) : [],
  }
}
export type Scoped = ReturnType<typeof scope>
