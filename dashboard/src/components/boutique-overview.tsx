import type { CSSProperties } from "react"
import { Boxes, ShoppingCart, ShieldCheck } from "lucide-react"
import { CardContent } from "@/components/ui/card"
import { Empty, Panel, Records, Status } from "@/components/records"
import { PodTable, WarningEvents } from "@/components/metrics"
import { time } from "@/lib/data"
import type { BoutiqueObservation, Cluster } from "@/lib/types"

const stageNames: Record<string, string> = {
  baseline: "Healthy baseline", "primary-fault": "Shipping fault", "crash-fault": "Shipping fault / crash scenario",
  verification: "Post-action verification",
}

export function BoutiqueOverview({ records, cluster, now, stale, playback=false, inspect }: {
  records: BoutiqueObservation[]; cluster?: Cluster; now: number; stale: boolean; playback?: boolean;
  inspect: (id: string) => void;
}) {
  // These are namespace-wide observations. Incident/workload filters must never
  // remove affected supporting services or mix another cluster into the view.
  const matching = records.filter(r => r.cluster_id === cluster?.cluster_id && r.namespace === cluster?.namespace)
  const inventory = matching.find(r => r.stage === "inventory")
  const journeys = matching.filter(r => r.journey !== null)
  const recent = (at: string) => {
    const elapsed = now - Date.parse(at)
    return !stale && !playback && Number.isFinite(elapsed) && elapsed >= 0 && elapsed <= 15000
  }
  return <div className="stagger space-y-6">
    <div className="advisory">
      <ShoppingCart className="mt-0.5 size-4 shrink-0 text-primary"/>
      <div><p className="text-sm font-semibold text-foreground">Connected Online Boutique</p>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">Eleven application and Redis Deployments. Fault injection and restricted recovery currently target shipping only. Planner proposals are supplied by the harness.</p>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">Deployment readiness is infrastructure evidence. Shopping requests are measured at experiment stages; they are not continuous availability monitoring. Formal recovery is decided separately by Verifier.</p></div>
    </div>
    <Panel title="Boutique service readiness" description={inventory ? `${inventory.cluster_id} / ${inventory.namespace} · Observed ${time(inventory.observed_at)}` : "Waiting for connected-mode Deployment inventory"}>
      {inventory?.services?.length ? <CardContent className="grid gap-3 p-5 sm:grid-cols-2 xl:grid-cols-3">{inventory.services.map(service => {
        const label = service.ready_state === null ? "UNKNOWN" : service.ready_state ? "READY" : "NotReady"
        const status = playback ? `Playback ${label}` : recent(inventory.observed_at) ? label : `Last known ${label}`
        const tone = service.ready_state === null ? "var(--warn)" : service.ready_state ? "var(--good)" : "var(--bad)"
        return <article key={service.name} className="svc min-w-0" data-target={service.recovery_target || undefined} style={{"--tone": tone} as CSSProperties} aria-label={`${service.name} readiness`}>
          <span aria-hidden className="svc-ring"/>
          <div className="relative flex flex-wrap items-center justify-between gap-2"><span className="flex min-w-0 items-center gap-2 font-mono text-[12px] font-medium"><Boxes className="size-3.5 shrink-0 text-muted-foreground"/>{service.name}</span><Status value={status}/></div>
          <p className="relative mt-5 text-xs text-muted-foreground">Ready <strong className="font-mono text-base text-foreground tabular-nums">{service.ready ?? "?"} / {service.desired ?? "?"}</strong> · Available {service.available ?? "?"}</p>
          <p className="relative mt-2 font-mono text-[10px] text-muted-foreground">Observed generation {service.observed_generation ?? "?"} / {service.generation ?? "?"}</p>
          {service.recovery_target && <p className="relative mt-3 flex items-center gap-1.5 text-[10px] font-medium text-primary"><ShieldCheck className="size-3"/>Restricted recovery target</p>}
        </article>
      })}</CardContent> : <Empty title="No Boutique inventory yet" detail="Run the connected experiment with the dashboard enabled. Missing observations are not healthy results."/>}
    </Panel>
    <Panel title="Shopping request results" description="Newest measured journey first · HTTP status codes · Recorded evidence, not current health">
      {journeys.length ? <Records headers={["STAGE / TIME", "HOME", "ADD TO CART", "CART", "CHECKOUT", "CONFIRMATION", "JOURNEY", "INCIDENT"]} rows={journeys.slice(0,20).map(record => {
        const journey = record.journey!
        const code = (value: number | null) => value === null ? "Unknown" : <span className={value >= 500 ? "font-mono text-bad" : value >= 400 ? "font-mono text-warn" : "font-mono"}>{value}</span>
        return [<div><p>{stageNames[record.stage] || record.stage}</p><p className="mt-1 font-mono text-[10px] text-muted-foreground">{time(record.observed_at)}</p></div>,
          code(journey.home_status), code(journey.add_to_cart_status), code(journey.cart_status),
          code(journey.checkout_status), journey.order_confirmed ? "Confirmed" : "Not confirmed",
          <Status value={journey.passed === null ? "UNKNOWN" : journey.passed ? "PASS" : "FAIL"}/>,
          record.incident_id ? <button type="button" onClick={() => inspect(record.incident_id!)} className="max-w-48 break-all text-left font-mono text-xs text-primary underline decoration-primary/40 underline-offset-4 transition-colors hover:decoration-primary">{record.incident_id}</button> : "Baseline"]
      })}/> : <Empty title="No shopping requests measured yet" detail="Baseline, injected-fault and post-action shopping measurements appear here as the experiment progresses."/>}
    </Panel>
    <PodTable cluster={cluster} transportStale={stale} demo={playback}/>
    <WarningEvents cluster={cluster}/>
    <p className="text-xs leading-relaxed text-muted-foreground">Pod status, restarts and warning events cover this namespace. Use the role pages for Sentinel activity and kubectl logs for individual application logs. Shopping measurements run at experiment stages; this view does not trace every application request or enable recovery of other services.</p>
  </div>
}
