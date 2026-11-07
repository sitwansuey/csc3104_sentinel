import type { ReactNode } from "react"
import { ShieldCheck } from "lucide-react"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { targetLabel, time } from "@/lib/data"
import type { Scoped } from "@/lib/data"
import type { WireMessage } from "@/lib/types"
import { decisionPresentation } from "@/lib/decision-display"

const GOOD = ["ALLOW","PASS","CLEAR","RECOVERED","APPLIED","NOOP","HEALTHY","READY","Ready","available","RECENT","RESOLVED"]
const BAD = ["FAIL","REJECT","REJECTED","FAILED","BLOCKED","CONFLICT","NOT_RECOVERED","error","Failed","CrashLoopBackOff","ImagePullBackOff","OOMKilled","NotReady","DENY","HIGH","CRITICAL"]
const INFO = ["VALIDATING","VERIFYING","EXECUTING","PROPOSED","PENDING","SCANNING","RECORDED","ACKNOWLEDGED","RUNNING"]
const WARN = ["ESCALATE","UNKNOWN","AWAITING_APPROVAL","NEEDS_REVIEW","NEEDS_RECONCILIATION","fault","STALE","Pending","unavailable","PARTIAL","BackOff","FailedScheduling","Unhealthy","WARNING","MEDIUM","FINDINGS","OPEN","NOT CONNECTED"]

/** Tone of a recorded status. Old or replayed observations never inherit the "current health" treatment. */
export function statusTone(value?: string | null): "good" | "bad" | "warn" | "stale" | "info" | "neutral" {
  const status = value || "NO DATA"
  if (/^(last known|stale|expired)/i.test(status)) return "stale"
  if (GOOD.includes(status)) return "good"
  if (BAD.includes(status)) return "bad"
  if (WARN.includes(status)) return "warn"
  if (INFO.includes(status)) return "info"
  return "neutral"
}

export function Status({ value }: { value?: string | null }) {
  const status = value || "NO DATA"
  const tone = statusTone(status)
  return <span data-slot="status" data-tone={tone} title={tone === "stale" ? "Not a current observation" : undefined} className="chip">{status}</span>
}
export function GuardDecisionStatus({ decision, demo, transportStale, now }: { decision:WireMessage | undefined; demo:boolean; transportStale?:boolean; now?:number }) {
  const presentation = decisionPresentation(decision, { demo, transportStale, now })
  const tone = presentation.tone === "danger" ? "bad" : presentation.tone === "warning" ? "warn" : presentation.tone
  return <Tooltip><TooltipTrigger asChild><span tabIndex={0} data-tone={tone} aria-label={`${presentation.label}. ${presentation.detail}`} className="chip cursor-help">{presentation.label}</span></TooltipTrigger><TooltipContent className="max-w-sm leading-relaxed" sideOffset={6}>{presentation.detail}</TooltipContent></Tooltip>
}
export function Panel({ title, description, children, className }: { title:string; description?:string; children:ReactNode; className?:string }) {
  return <Card className={cn("panel gap-0 overflow-hidden border-border py-0 shadow-none",className)}>
    <div data-slot="card-header" className="panel-head"><div data-slot="card-title" className="panel-title">{title}</div>{description&&<div data-slot="card-description" className="pl-4 text-xs leading-relaxed text-muted-foreground">{description}</div>}</div>
    {children}
  </Card>
}
export function Empty({ title="No records yet", detail="Waiting for matching records from the configured producer." }: { title?:string; detail?:string }) {
  return <div className="flex min-h-44 flex-col items-center justify-center gap-4 px-6 py-9 text-center"><div className="radar" aria-hidden><span/><span/><span/><i className="radar-core"/></div><div><p className="text-sm font-medium">{title}</p><p className="mx-auto mt-2 max-w-md text-xs leading-relaxed text-muted-foreground">{detail}</p></div></div>
}
export function Records({ headers, rows }: { headers:string[]; rows:ReactNode[][] }) {
  if(!rows.length)return <Empty/>
  return <Table><TableHeader><TableRow className="border-border hover:bg-transparent">{headers.map(h=><TableHead key={h} className="h-10 whitespace-nowrap bg-foreground/[.025] dark:bg-white/[.015] px-4 text-[9.5px] uppercase tracking-[.16em] text-muted-foreground first:pl-5 last:pr-5">{h}</TableHead>)}</TableRow></TableHeader><TableBody>{rows.map((row,i)=><TableRow key={i} className="border-border/70 hover:bg-primary/[.035]">{row.map((cell,j)=><TableCell key={j} className="px-4 py-3.5 text-xs first:pl-5 last:pr-5">{cell??"—"}</TableCell>)}</TableRow>)}</TableBody></Table>
}
/** Free-form producer text that may wrap inside a table cell instead of widening the table. */
export function Prose({ children }: { children:ReactNode }) { return <span className="block min-w-48 whitespace-normal break-words leading-relaxed">{children===undefined||children===null||children===""?"—":children}</span> }
export function Context({ children }: { children:ReactNode }) { return <div role="note" className="advisory"><ShieldCheck className="mt-0.5 size-4 shrink-0 text-primary"/><div>{children}</div></div> }
export function Json({ value, label="Inspect recorded JSON" }: { value:unknown; label?:string }) { return <details className="disclosure overflow-hidden rounded-lg border border-border bg-[var(--surface-inset)]"><summary className="cursor-pointer px-4 py-3 font-mono text-[11px] tracking-wide text-muted-foreground transition-colors hover:text-foreground">{label}</summary><pre className="max-h-96 overflow-auto border-t border-border bg-[var(--surface-code)] p-4 font-mono text-[11px] leading-relaxed text-foreground/85">{JSON.stringify(value,null,2)}</pre></details> }
export function Fields({ entries }: { entries:[string,ReactNode][] }) { return <dl className="grid grid-cols-[150px_minmax(0,1fr)] gap-x-5 gap-y-3.5 text-xs max-sm:grid-cols-1">{entries.map(([label,value])=><div className="contents" key={label}><dt className="font-mono text-[10px] uppercase tracking-[.14em] text-muted-foreground">{label}</dt><dd className="break-all leading-relaxed">{value??"Not supplied"}</dd></div>)}</dl> }
export function ActivityFeed({ data, demo=false, transportStale=false, now }: { data:Scoped; demo?:boolean; transportStale?:boolean; now?:number }) {
  const names:Record<string,string>={incident:"Failure detected",proposal:"Recovery proposed",guard_result:"Guard decision",dependency_result:"Dependencies checked",execution_result:"Execution result",outcome:"Outcome verified"}
  return <Panel title="Recovery activity" description="Newest stored message first; timestamps are supplied by producers.">{data.messages.length?<ol className="timeline px-5 py-2">{data.messages.slice(0,8).map(({sequence,message:m})=><li key={sequence} className="flex gap-4 py-3"><span className="tl-node"/><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs font-medium">{names[m.kind]||m.kind}</span>{m.kind === "guard_result" ? <GuardDecisionStatus decision={m} demo={demo} transportStale={transportStale} now={now}/> : <Status value={m.verdict||m.status||m.action||"OPEN"}/>}</div><p className="mt-1.5 truncate text-[11px] text-muted-foreground">{m.reason_codes?.join(" · ")||m.rationale||targetLabel(m.target)}</p><p className="mt-2 font-mono text-[10px] text-muted-foreground dark:text-muted-foreground/70">{time(m.checked_at||m.recorded_at||m.window_end||m.observed_at)} · #{sequence}</p></div></li>)}</ol>:<Empty title="No recovery events yet"/>}</Panel>
}
export function EventFeed({ data, member }: { data:Scoped; member?:number }) {
  const events=data.telemetry.filter(t=>!["observation","heartbeat"].includes(t.event_type)&&(!member||t.component===member))
  return <Panel title="Fault markers & component errors" description="Markers record an injection; they do not perform one."><Records headers={["TYPE","SUMMARY","SOURCE","TIME"]} rows={events.slice(0,40).map(e=>[<Status value={e.event_type}/>,<Prose>{e.summary}</Prose>,`Member ${e.component} · ${targetLabel(e.target)}`,time(e.at)])}/></Panel>
}
