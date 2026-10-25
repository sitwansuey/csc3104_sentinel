import type { ReactNode } from "react"
import { Activity, Radio, ShieldCheck } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { targetLabel, time } from "@/lib/data"
import type { Scoped } from "@/lib/data"
import type { WireMessage } from "@/lib/types"
import { decisionPresentation } from "@/lib/decision-display"

export function Status({ value }: { value?: string | null }) {
  const status=value || "NO DATA"
  const good=["ALLOW","PASS","CLEAR","RECOVERED","APPLIED","NOOP","HEALTHY","READY","Ready","available","RECENT"].includes(status)
  const bad=["FAIL","REJECT","REJECTED","FAILED","BLOCKED","CONFLICT","NOT_RECOVERED","error","Failed","CrashLoopBackOff","ImagePullBackOff","OOMKilled","NotReady"].includes(status)
  const warn=["ESCALATE","UNKNOWN","AWAITING_APPROVAL","NEEDS_REVIEW","NEEDS_RECONCILIATION","fault","STALE","Pending","unavailable","PARTIAL","BackOff","FailedScheduling","Unhealthy"].includes(status)
  // Old or replayed observations must never inherit the green "current health" treatment.
  const stale=/^(last known|stale|expired)/i.test(status)
  return <Badge variant="outline" title={stale?"Not a current observation":undefined} className={cn("rounded-md font-mono text-[10px] font-medium tracking-wide",stale&&"border-dashed border-amber-500/40 bg-transparent text-amber-600 dark:text-amber-300",!stale&&good&&"border-emerald-500/20 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300",!stale&&bad&&"border-rose-500/20 bg-rose-500/10 text-rose-600 dark:text-rose-300",!stale&&warn&&"border-amber-500/20 bg-amber-500/10 text-amber-600 dark:text-amber-300")}>{status}</Badge>
}
export function GuardDecisionStatus({ decision, demo, transportStale, now }: { decision:WireMessage | undefined; demo:boolean; transportStale?:boolean; now?:number }) {
  const presentation = decisionPresentation(decision, { demo, transportStale, now })
  return <Tooltip><TooltipTrigger asChild><Badge variant="outline" tabIndex={0} aria-label={`${presentation.label}. ${presentation.detail}`} className={cn("cursor-help rounded-md font-mono text-[10px] font-medium tracking-wide focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",presentation.tone === "good"&&"border-emerald-500/20 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300",presentation.tone === "danger"&&"border-rose-500/20 bg-rose-500/10 text-rose-600 dark:text-rose-300",presentation.tone === "warning"&&"border-amber-500/20 bg-amber-500/10 text-amber-600 dark:text-amber-300",presentation.tone === "neutral"&&"border-border bg-muted/40 text-muted-foreground")}>{presentation.label}</Badge></TooltipTrigger><TooltipContent className="max-w-sm leading-relaxed" sideOffset={6}>{presentation.detail}</TooltipContent></Tooltip>
}
export function Panel({ title, description, children, className }: { title:string; description?:string; children:ReactNode; className?:string }) {
  return <Card className={cn("gap-0 overflow-hidden border-border/70 py-0 shadow-none",className)}><CardHeader className="border-b border-border/60 px-5 py-4"><CardTitle className="text-sm font-medium">{title}</CardTitle>{description&&<CardDescription className="text-xs">{description}</CardDescription>}</CardHeader>{children}</Card>
}
export function Empty({ title="No records yet", detail="Waiting for matching records from the configured producer." }: { title?:string; detail?:string }) {
  return <div className="flex min-h-40 flex-col items-center justify-center gap-3 px-6 py-8 text-center"><div className="rounded-xl border bg-muted/50 p-3"><Radio className="size-5 text-muted-foreground"/></div><div><p className="text-sm font-medium">{title}</p><p className="mx-auto mt-2 max-w-md text-xs leading-relaxed text-muted-foreground">{detail}</p></div></div>
}
export function Records({ headers, rows }: { headers:string[]; rows:ReactNode[][] }) {
  if(!rows.length)return <Empty/>
  return <Table><TableHeader><TableRow className="hover:bg-transparent">{headers.map(h=><TableHead key={h} className="h-10 whitespace-nowrap px-4 text-[10px] first:pl-5 last:pr-5 tracking-wider text-muted-foreground">{h}</TableHead>)}</TableRow></TableHeader><TableBody>{rows.map((row,i)=><TableRow key={i}>{row.map((cell,j)=><TableCell key={j} className="px-4 py-3.5 text-xs first:pl-5 last:pr-5">{cell??"—"}</TableCell>)}</TableRow>)}</TableBody></Table>
}
/** Free-form producer text that may wrap inside a table cell instead of widening the table. */
export function Prose({ children }: { children:ReactNode }) { return <span className="block min-w-48 whitespace-normal break-words leading-relaxed">{children===undefined||children===null||children===""?"—":children}</span> }
export function Context({ children }: { children:ReactNode }) { return <Alert className="border-border/70 bg-muted/20 text-muted-foreground"><ShieldCheck className="size-4"/><AlertDescription className="text-xs leading-relaxed">{children}</AlertDescription></Alert> }
export function Json({ value, label="Inspect recorded JSON" }: { value:unknown; label?:string }) { return <details className="rounded-lg border border-border/70"><summary className="cursor-pointer px-4 py-3 text-xs text-muted-foreground">{label}</summary><pre className="max-h-96 overflow-auto border-t bg-muted/30 p-4 font-mono text-[11px] leading-relaxed">{JSON.stringify(value,null,2)}</pre></details> }
export function Fields({ entries }: { entries:[string,ReactNode][] }) { return <dl className="grid grid-cols-[140px_minmax(0,1fr)] gap-x-5 gap-y-3 text-xs max-sm:grid-cols-1">{entries.map(([label,value])=><div className="contents" key={label}><dt className="text-muted-foreground">{label}</dt><dd className="break-all leading-relaxed">{value??"Not supplied"}</dd></div>)}</dl> }
export function ActivityFeed({ data, demo=false, transportStale=false, now }: { data:Scoped; demo?:boolean; transportStale?:boolean; now?:number }) {
  const names:Record<string,string>={incident:"Failure detected",proposal:"Recovery proposed",guard_result:"Guard decision",dependency_result:"Dependencies checked",execution_result:"Execution result",outcome:"Outcome verified"}
  return <Panel title="Recovery activity" description="Newest stored message first; timestamps are supplied by producers.">{data.messages.length?<div className="divide-y divide-border/50">{data.messages.slice(0,8).map(({sequence,message:m})=><div key={sequence} className="flex gap-3 px-5 py-4"><div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg border bg-muted/40"><Activity className="size-3.5 text-primary"/></div><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs font-medium">{names[m.kind]||m.kind}</span>{m.kind === "guard_result" ? <GuardDecisionStatus decision={m} demo={demo} transportStale={transportStale} now={now}/> : <Status value={m.verdict||m.status||m.action||"OPEN"}/>}</div><p className="mt-1.5 truncate text-[11px] text-muted-foreground">{m.reason_codes?.join(" · ")||m.rationale||targetLabel(m.target)}</p><p className="mt-2 font-mono text-[10px] text-muted-foreground/70">{time(m.checked_at||m.recorded_at||m.window_end||m.observed_at)} · #{sequence}</p></div></div>)}</div>:<Empty title="No recovery events yet"/>}</Panel>
}
export function EventFeed({ data, member }: { data:Scoped; member?:number }) {
  const events=data.telemetry.filter(t=>!["observation","heartbeat"].includes(t.event_type)&&(!member||t.component===member))
  return <Panel title="Fault markers & component errors" description="Markers record an injection; they do not perform one."><Records headers={["TYPE","SUMMARY","SOURCE","TIME"]} rows={events.slice(0,40).map(e=>[<Status value={e.event_type}/>,<Prose>{e.summary}</Prose>,`Member ${e.component} · ${targetLabel(e.target)}`,time(e.at)])}/></Panel>
}
