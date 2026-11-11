import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react"
import { Activity, Boxes, ChevronRight, Clock3, FlaskConical, GitBranch, LayoutDashboard, Menu, PanelRightOpen, Pause, Play, Radio, RefreshCw, Server, Shield, ShieldCheck, Sparkles, Terminal, Workflow, type LucideIcon } from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { CardContent } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { ClusterScope, ClusterStats, NodeGrid, PodTable, ReadinessChart, ResourceChart, Sources, WarningEvents } from "@/components/metrics"
import { Context, EventFeed, Fields, GuardDecisionStatus, Json, Panel, Prose, Records, Status } from "@/components/records"
import { RecoveryOverview } from "@/components/recovery-overview"
import { IncidentDetails } from "@/components/incident-details"
import { LabExperiment } from "@/components/lab-experiment"
import { BoutiqueOverview } from "@/components/boutique-overview"
import { SecurityOverview } from "@/components/security-overview"
import { ThemeToggle } from "@/components/theme-toggle"
import { age, fresh, identity, observedClusters, scope, targetLabel, time, type Scoped } from "@/lib/data"
import { decisionPresentation } from "@/lib/decision-display"
import type { ChartRange } from "@/lib/chart-data"
import { cn } from "@/lib/utils"
import type { Snapshot, View } from "@/lib/types"

type Group = "workspace" | "pipeline" | "defense"
const views:{id:View;label:string;icon:LucideIcon;member?:number;group:Group;description:string}[]=[
  {id:"overview",label:"Overview",icon:LayoutDashboard,group:"workspace",description:"Follow a service failure through repair and verified recovery."},
  {id:"cluster",label:"Kubernetes",icon:Boxes,group:"workspace",description:"Measured resources, node capacity and namespace Pod health."},
  {id:"boutique",label:"Online Boutique",icon:GitBranch,group:"workspace",description:"Service readiness and measured shopping behavior across the connected application."},
  {id:"coordinator",label:"Coordinator",icon:Workflow,member:1,group:"pipeline",description:"Incident states, recorded approvals and durable routing."},
  {id:"monitoring",label:"Monitoring",icon:Activity,member:2,group:"pipeline",description:"Deployment observations and persistent failure evidence."},
  {id:"planner",label:"Planner / RCA",icon:Sparkles,member:3,group:"pipeline",description:"Recorded recovery proposals, reasoning and evidence references."},
  {id:"guard",label:"Recovery Guard",icon:ShieldCheck,member:4,group:"pipeline",description:"Policy findings, approval requirements and expiring decisions."},
  {id:"executor",label:"Executor",icon:Terminal,member:5,group:"pipeline",description:"Reported attempts, conflicts and reconciliation requirements."},
  {id:"verification",label:"Dependencies / Health",icon:GitBranch,member:6,group:"pipeline",description:"Dependency findings and post-execution health windows."},
  {id:"security",label:"Security",icon:Shield,group:"defense",description:"gRPC inspection decisions, image CVEs and package patch recommendations."},
]
const groups:{id:Group;label:string}[]=[{id:"workspace",label:"Workspace"},{id:"pipeline",label:"Recovery pipeline"},{id:"defense",label:"Defense"}]
type LinkState = "live" | "paused" | "lost" | "demo"
const linkText:Record<LinkState,string>={live:"Refreshing · 2s",paused:"Paused",lost:"Disconnected",demo:"Demo playback"}
const uplinkText:Record<LinkState,string>={live:"Live signal · sampling every 2s",paused:"Polling halted by operator",lost:"Signal lost · last known records",demo:"Fixture playback · synthetic data"}

function initialView():View { const value=location.hash.slice(1);return views.some(v=>v.id===value)?value as View:"overview" }
export default function App() {
  const [view,setView]=useState<View>(initialView),[snapshot,setSnapshot]=useState<Snapshot|null>(null)
  const [error,setError]=useState<string|null>(null),[paused,setPaused]=useState(false),[loading,setLoading]=useState(true)
  const [incident,setIncident]=useState("all"),[workload,setWorkload]=useState("all"),[mobileOpen,setMobileOpen]=useState(false)
  const [cluster,setCluster]=useState("all"),[range,setRange]=useState<ChartRange>("all"),[selectedIncident,setSelectedIncident]=useState<string|null>(null)
  const [now,setNow]=useState(Date.now),[spins,setSpins]=useState(0)
  const controller=useRef<AbortController|null>(null),clusterChosen=useRef(false)
  const refresh=useCallback(async()=>{
    if(controller.current)return
    const current=new AbortController();controller.current=current
    const timeout=setTimeout(()=>current.abort(),5000)
    try {
      const response=await fetch("/api/snapshot",{cache:"no-store",signal:current.signal})
      if(!response.ok)throw new Error("Snapshot unavailable")
      const data=await response.json() as Snapshot
      if(controller.current===current&&!current.signal.aborted){setSnapshot(data);setNow(Date.now());setError(null)}
    } catch { if(controller.current===current)setError("Dashboard connection lost. Displayed records may be stale.") }
    finally {
      clearTimeout(timeout)
      if(controller.current===current){controller.current=null;setLoading(false)}
    }
  },[])
  useEffect(()=>{ if(paused)return;void refresh();const timer=setInterval(()=>void refresh(),2000);return()=>clearInterval(timer) },[paused,refresh])
  useEffect(()=>()=>{controller.current?.abort();controller.current=null},[])
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer)},[])
  const togglePaused=()=>{
    if(!paused){const pending=controller.current;controller.current=null;pending?.abort()}
    setPaused(!paused)
  }
  const navigate=(next:View)=>{setView(next);setMobileOpen(false);history.replaceState(null,"",`#${next}`)}
  const current=views.find(v=>v.id===view)!
  const data=useMemo(()=>snapshot?scope(snapshot,incident==="all"?"":incident,workload==="all"?"":workload,cluster==="all"?"":cluster):null,[snapshot,incident,workload,cluster])
  const clusters=useMemo(()=>snapshot?observedClusters(snapshot,now):[],[snapshot,now])
  const targets=useMemo(()=>{
    const result=new Map<string,Snapshot["incidents"][number]["target"]>()
    snapshot?.incidents.forEach(i=>result.set(identity(i.target),i.target))
    snapshot?.telemetry.forEach(e=>{if(e.target)result.set(identity(e.target),e.target)})
    return [...result.entries()]
  },[snapshot])
  useEffect(()=>{if(!clusterChosen.current&&cluster==="all"&&clusters.length){clusterChosen.current=true;setCluster(clusters[0].id)}},[clusters,cluster])
  const chooseCluster=(next:string)=>{clusterChosen.current=true;setCluster(next);setIncident("all");setWorkload("all");setSelectedIncident(null)}
  const chooseIncident=(next:string)=>{
    setIncident(next);setWorkload("all");setSelectedIncident(null)
    const target=snapshot?.incidents.find(i=>i.incident_id===next)?.target
    if(target){clusterChosen.current=true;setCluster(target.cluster_id)}
  }
  const chooseWorkload=(next:string)=>{
    setWorkload(next);setSelectedIncident(null)
    const target=targets.find(([key])=>key===next)?.[1]
    if(target){clusterChosen.current=true;setCluster(target.cluster_id)}
    if(target&&incident!=="all"&&identity(snapshot?.incidents.find(i=>i.incident_id===incident)?.target)!==next)setIncident("all")
  }
  const inspectIncident=(id:string)=>{
    const target=snapshot?.incidents.find(i=>i.incident_id===id)?.target
    if(target){clusterChosen.current=true;setCluster(target.cluster_id)}
    setIncident(id);setWorkload("all");setSelectedIncident(id)
  }
  const demo=snapshot?.mode==="synthetic",transportStale=Boolean(error)
  const unavailableSources=Object.entries(snapshot?.sources||{}).filter(([key,source])=>key!=="cluster_metrics"&&source.connected===false)
  const link:LinkState=error?"lost":paused?"paused":demo?"demo":"live"
  const selectedCluster=clusters.find(c=>c.id===cluster)
  const incidentOptions=(snapshot?.incidents||[]).filter(i=>cluster==="all"||i.target.cluster_id===cluster).map(i=>[i.incident_id,i.incident_id] as [string,string])
  const workloadOptions=targets.filter(([,t])=>cluster==="all"||t.cluster_id===cluster).map(([key,t])=>[key,`${t.cluster_id} · ${targetLabel(t)}`] as [string,string])
  const labArmed=Boolean(snapshot?.lab&&!demo)
  const title=view==="overview"?"System overview":view==="cluster"?"Cluster intelligence":current.label
  const eyebrow=current.member?`Pipeline · Role 0${current.member}`:current.group==="defense"?"Defense · Recorded evidence":"Sentinel observability"
  const mobileNav=<div className="space-y-1">{groups.map(g=><div key={g.id}><p className="rail-group">{g.label}</p>{views.filter(v=>v.group===g.id).map(v=><Button key={v.id} aria-label={v.label} variant={view===v.id?"secondary":"ghost"} onClick={()=>navigate(v.id)} className="h-10 w-full justify-start gap-3"><v.icon className="size-4"/><span>{v.label}</span>{v.member&&<span className="rail-code">0{v.member}</span>}</Button>)}</div>)}</div>
  return <TooltipProvider><Tabs orientation="vertical" value={view} onValueChange={value=>navigate(value as View)} className="min-h-screen items-start gap-0">
    <div className="ambient" data-tone={link} aria-hidden><div className="aurora aurora-a"/><div className="aurora aurora-b"/><div className="aurora aurora-c"/><div className="beam"/></div>
    <aside className="rail fixed inset-y-0 left-0 z-30 hidden w-[264px] flex-col overflow-y-auto lg:flex"><Brand/><Rail view={view}/><div className="mt-auto px-4 pb-5 pt-6"><Uplink state={link} now={now}/><p className="mt-4 px-1 font-mono text-[9px] tracking-[.22em] text-muted-foreground dark:text-muted-foreground/55">OBSERVE · VALIDATE · RECOVER</p></div></aside>
    <div className="relative z-10 min-w-0 flex-1 lg:ml-[264px]">
      <header className="topbar sticky top-0 z-20 flex h-14 items-center justify-between gap-3 px-4 sm:px-7 lg:px-10"><div className="flex min-w-0 items-center gap-2.5"><Sheet open={mobileOpen} onOpenChange={setMobileOpen}><SheetTrigger asChild><Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation"><Menu className="size-4"/></Button></SheetTrigger><SheetContent side="left" className="w-72 overflow-y-auto"><SheetHeader><SheetTitle className="font-display text-2xl font-normal italic">Sentinel workspace</SheetTitle></SheetHeader><div className="px-3">{mobileNav}</div></SheetContent></Sheet><span className="hidden font-mono text-[11px] tracking-[.14em] text-muted-foreground sm:block">SENTINEL</span><ChevronRight className="hidden size-3 text-muted-foreground/50 sm:block"/><span className="truncate text-[13px] font-medium">{current.label}</span></div><div className="flex items-center gap-2"><ThemeToggle/><span className="live-pill mr-1 hidden sm:inline-flex" data-state={link}><span className="beacon" data-still={link==="paused"||undefined} style={{color:"var(--signal)"}}/>{linkText[link]}</span><Tooltip><TooltipTrigger asChild><Button variant="outline" size="sm" onClick={togglePaused} aria-label={paused?"Resume updates":"Pause updates"} className="border-border bg-transparent dark:bg-transparent">{paused?<Play className="size-3.5"/>:<Pause className="size-3.5"/>}<span className="hidden sm:inline">{paused?"Resume":"Pause"}</span></Button></TooltipTrigger><TooltipContent>{paused?"Resume automatic polling":"Pause automatic polling"}</TooltipContent></Tooltip><Button variant="outline" size="sm" onClick={()=>{setSpins(n=>n+1);void refresh()}} aria-label="Refresh dashboard" className="border-border bg-transparent dark:bg-transparent"><RefreshCw key={spins} className={cn("size-3.5",spins>0&&"spin-once")}/><span className="hidden sm:inline">Refresh</span></Button></div></header>
      <main className="mx-auto max-w-[1680px] space-y-7 px-4 pb-10 pt-9 sm:px-7 lg:px-10">
        <section className="flex flex-wrap items-end justify-between gap-6"><div className="min-w-0 max-w-3xl"><p className="eyebrow">{eyebrow}</p><h1 key={view} className="display-title mt-5">{title}</h1><p className="mt-4 max-w-xl text-sm leading-relaxed text-muted-foreground">{current.description}</p></div><span className="stamp" data-armed={labArmed||undefined}><Shield className="size-3.5"/>{labArmed?"LAB CONTROLS ENABLED":"READ ONLY"}</span></section>
        {error&&<Alert variant="destructive" className="rise border-bad/40 bg-bad/[.07] text-bad"><Radio className="size-4"/><AlertDescription className="text-bad/90">{error} {paused?"Automatic retries are paused. Refresh manually or resume updates.":"Retrying automatically."}{snapshot&&` Last snapshot received ${age(snapshot.generated_at)}; health and decision badges below are marked as last known.`}</AlertDescription></Alert>}
        {demo&&<Alert className="border-warn/25 bg-warn/[.06] py-2.5 text-warn"><FlaskConical className="size-4"/><AlertDescription className="text-[11.5px] text-warn/85"><p><strong className="mr-1 font-mono font-semibold tracking-widest">DEMO</strong> · {snapshot?.demo_step} · All cluster measurements and recovery evidence are synthetic.</p></AlertDescription></Alert>}
        {!loading&&snapshot&&!demo&&unavailableSources.length>0&&<Context><p>Some recorded data sources are unavailable.</p>{unavailableSources.map(([name,source])=><p key={name}><strong>{({coordinator:"Coordinator",telemetry:"Telemetry",boutique:"Online Boutique",security:"Security evidence"} as Record<string,string>)[name]||name}:</strong> {source.detail||"No readable source is connected. Retrying."}</p>)}</Context>}
        {view==="overview"&&snapshot?.lab&&!demo&&<LabExperiment lab={snapshot.lab} stale={transportStale} paused={paused} now={now} refresh={()=>void refresh()} inspect={inspectIncident}/>}
        <section aria-label="Scope controls" className="console"><div className="console-cell"><ClusterSelect value={cluster} onChange={chooseCluster} clusters={clusters} selected={selectedCluster} transportStale={transportStale}/></div><div className="console-cell"><Filter label="Incident" value={incident} onChange={chooseIncident} placeholder="All recent incidents" options={incidentOptions}/></div><div className="console-cell"><Filter label="Workload" value={workload} onChange={chooseWorkload} placeholder="All observed workloads" options={workloadOptions}/></div><div className="console-cell"><RangeSelect value={range} onChange={setRange}/></div><div className="console-cell ml-auto border-r-0 text-right max-sm:ml-0 max-sm:text-left"><p className="console-label sm:justify-end"><Radio className="size-3"/>Last sample</p><p className="font-mono text-xs tabular-nums">{paused?"Updates paused":snapshot?`Snapshot ${time(snapshot.generated_at)}`:"Waiting for snapshot"}</p><p className="mt-1.5 font-mono text-[9.5px] text-muted-foreground dark:text-muted-foreground/70">2s polling · recent source records</p></div></section>
        {loading||!snapshot||!data?<div className="space-y-5" aria-label="Loading dashboard"><div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{[1,2,3,4].map(i=><Skeleton key={i} className="skeleton-shimmer h-36 rounded-xl"/>)}</div><Skeleton className="skeleton-shimmer h-80 rounded-xl"/></div>:views.map(v=><TabsContent key={v.id} value={v.id} className="stagger m-0 space-y-6"><ViewContent view={v.id} snapshot={snapshot} data={data} navigate={navigate} range={range} now={now} transportStale={transportStale} inspect={inspectIncident}/></TabsContent>)}
        {snapshot&&<IncidentDetails snapshot={snapshot} incidentId={selectedIncident} open={selectedIncident!==null} onOpenChange={open=>{if(!open)setSelectedIncident(null)}}/>}
        <footer className="flex flex-wrap justify-between gap-3 border-t border-border pt-6 font-mono text-[10px] tracking-wide text-muted-foreground dark:text-muted-foreground/70"><span>Sentinel <span className="mx-2 text-border">/</span> Incident recovery workspace</span><span>Signals inform decisions. Execution stays with the recovery workflow.</span></footer>
      </main>
    </div>
  </Tabs></TooltipProvider>
}

function Brand(){return <div className="flex items-center gap-3.5 px-5 pb-3 pt-6"><span className="mark" aria-hidden><span className="mark-sweep"/><span className="mark-core"/></span><div><p className="font-mono text-[12px] font-semibold tracking-[.34em]">SENTINEL</p><p className="mt-1 font-display text-[15px] italic leading-none text-muted-foreground">Recovery workspace</p></div></div>}

/** Rail navigation. The active indicator is one shared element that slides between tabs (transform only). */
function Rail({view}:{view:View}){
  const root=useRef<HTMLDivElement>(null),first=useRef(true)
  const [indicator,setIndicator]=useState<{top:number;instant:boolean}|null>(null)
  useLayoutEffect(()=>{
    const element=root.current
    if(!element)return
    const measure=()=>{
      const active=element.querySelector<HTMLElement>('[role="tab"][data-state="active"]')
      if(!active)return
      let top=0,node:HTMLElement|null=active
      while(node&&node!==element){top+=node.offsetTop;node=node.offsetParent as HTMLElement|null}
      setIndicator({top,instant:first.current});first.current=false
    }
    measure()
    const observer=new ResizeObserver(measure);observer.observe(element)
    return()=>observer.disconnect()
  },[view])
  return <div ref={root} className="relative px-4"><span aria-hidden className="rail-indicator" data-instant={indicator?.instant||undefined} style={{transform:`translateY(${indicator?.top??0}px)`,opacity:indicator?1:0,left:16,right:16}}/>
    <TabsList className="h-auto w-full flex-col items-stretch justify-start gap-0 rounded-none bg-transparent p-0">{groups.map(g=><div key={g.id}><p className="rail-group">{g.label}</p><div className="rail-items" data-wire={g.id==="pipeline"||undefined}>{views.filter(v=>v.group===g.id).map(v=><TabsTrigger key={v.id} value={v.id} aria-label={v.label} data-pipeline={v.member?"":undefined} className="rail-tab w-full flex-none justify-start"><v.icon className="size-4"/><span className="truncate">{v.label}</span>{v.member&&<span className="rail-code">0{v.member}</span>}</TabsTrigger>)}</div></div>)}</TabsList>
  </div>
}

const beat=(o:number)=>`H${o+34} L${o+38} 17 L${o+42} 20 H${o+50} L${o+54} 5 L${o+58} 31 L${o+62} 14 L${o+65} 20 H${o+80} L${o+84} 17.5 L${o+88} 20 H${o+100}`
const jitter=(o:number)=>`H${o+20} L${o+24} 13 L${o+27} 26 L${o+31} 17 L${o+34} 23 H${o+66} L${o+69} 15 L${o+72} 24 H${o+100}`
const traces:Record<LinkState,string>={live:`M0 20 ${[0,100,200,300].map(beat).join(" ")}`,demo:`M0 20 ${[0,100,200,300].map(beat).join(" ")}`,paused:"M0 20 H400",lost:`M0 20 ${[0,100,200,300].map(jitter).join(" ")}`}
/** The heartbeat only beats while polling is live; it flatlines when paused and stutters red when the link drops. */
function Uplink({state,now}:{state:LinkState;now:number}){
  const clock=new Date(now).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false})
  return <div className="uplink" data-state={state}><div className="flex items-center justify-between"><span className="font-mono text-[9.5px] tracking-[.22em] text-muted-foreground">UPLINK</span><span className="font-mono text-[10.5px] tabular-nums text-foreground/80">{clock}</span></div><svg viewBox="0 0 200 34" preserveAspectRatio="none" className="ecg" aria-hidden><g className="ecg-track"><path d={traces[state]} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke"/></g></svg><p className="text-[11px] leading-snug text-muted-foreground">{uplinkText[state]}</p></div>
}

function Filter({label,value,onChange,placeholder,options}:{label:string;value:string;onChange:(value:string)=>void;placeholder:string;options:[string,string][]}){return <div className="min-w-0"><p className="console-label">{label}</p><Select value={value} onValueChange={onChange}><SelectTrigger aria-label={`${label} filter`} className="h-7 w-[200px] max-w-[65vw] border-0 bg-transparent px-0 text-[13px] font-medium shadow-none dark:bg-transparent dark:hover:bg-transparent"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="all">{placeholder}</SelectItem>{options.map(([v,l])=><SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent></Select></div>}
const clusterTone:Record<string,string>={Receiving:"good",Demo:"neutral",Stale:"warn","Unknown time":"warn",Unavailable:"bad","No sample":"neutral"}
type ClusterOption=ReturnType<typeof observedClusters>[number]
function ClusterSelect({value,onChange,clusters,selected,transportStale}:{value:string;onChange:(value:string)=>void;clusters:ClusterOption[];selected?:ClusterOption;transportStale:boolean}){
  const status=transportStale&&selected?"Stale":selected?.status||"No sample"
  return <div className="min-w-0"><p className="console-label"><Server className="size-3"/>Cluster</p><div className="flex items-center gap-3"><Select value={value} onValueChange={onChange}><SelectTrigger aria-label="Cluster filter" className="h-7 w-[170px] max-w-[60vw] border-0 bg-transparent px-0 text-[13px] font-semibold shadow-none dark:bg-transparent dark:hover:bg-transparent"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="all">All observed clusters</SelectItem>{clusters.map(c=><SelectItem key={c.id} value={c.id}>{c.id}</SelectItem>)}</SelectContent></Select>{selected?<Tooltip><TooltipTrigger asChild><span tabIndex={0} aria-label={`Cluster connection: ${status}`} data-tone={clusterTone[status]||"neutral"} className="chip cursor-help">{status}</span></TooltipTrigger><TooltipContent className="max-w-xs leading-relaxed">{selected.observedAt?`Last cluster sample ${age(selected.observedAt)} (${time(selected.observedAt)}).`:"No cluster sample has been collected."}{transportStale?" The dashboard connection is lost, so this may not be current.":" Status describes received telemetry, not the cluster itself."}</TooltipContent></Tooltip>:<span className="chip" data-tone="neutral">{clusters.length} observed</span>}</div></div>
}
const ranges:{id:ChartRange;label:string}[]=[{id:"5m",label:"5m"},{id:"15m",label:"15m"},{id:"1h",label:"1h"},{id:"all",label:"All"}]
function RangeSelect({value,onChange}:{value:ChartRange;onChange:(value:ChartRange)=>void}){const index=Math.max(0,ranges.findIndex(r=>r.id===value));return <div className="min-w-0"><p className="console-label"><Clock3 className="size-3"/>Chart range</p><div role="group" aria-label="Chart time range" className="seg"><span aria-hidden className="seg-pill" style={{"--i":index} as CSSProperties}/>{ranges.map(r=><button key={r.id} type="button" aria-pressed={value===r.id} onClick={()=>onChange(r.id)} className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{r.label}</button>)}</div></div>}
type Live={demo:boolean;transportStale:boolean;now:number}
type ViewProps={view:View;snapshot:Snapshot;data:Scoped;navigate:(view:View)=>void;range:ChartRange;now:number;transportStale:boolean;inspect:(id:string)=>void}
function ViewContent({view,snapshot,data,navigate,range,now,transportStale,inspect}:ViewProps) {
  const cluster=data.clusters[0],demo=snapshot.mode==="synthetic",live={demo,transportStale}
  if(view==="boutique")return <BoutiqueOverview records={snapshot.boutique_history||[]} cluster={cluster} now={now} stale={transportStale} playback={demo} inspect={inspect}/>
  if(view==="security")return <SecurityOverview events={snapshot.security_events||[]} now={now}/>
  if(view==="overview")return <RecoveryOverview data={data} snapshot={snapshot} range={range} now={now} transportStale={transportStale} navigate={navigate} inspect={inspect}/>
  if(view==="cluster")return <><ClusterScope cluster={cluster} {...live}/><ClusterStats clusters={data.clusters} {...live}/><div className="grid gap-5 xl:grid-cols-[minmax(0,1.7fr)_minmax(300px,1fr)]"><ResourceChart clusters={data.clusters} telemetry={data.telemetry} range={range} demo={demo}/><Sources cluster={cluster} {...live}/></div><NodeGrid cluster={cluster} {...live}/><PodTable cluster={cluster} {...live}/><WarningEvents cluster={cluster}/>{cluster&&Object.values(cluster.truncation).some(n=>n>0)&&<Context>Inventory exceeded collector limits. Lists and aggregates may be incomplete.</Context>}</>
  return <><RecoveryStats data={data} {...live} now={now}/>{view==="coordinator"?<Coordinator snapshot={snapshot} data={data} now={now} inspect={inspect}/>:view==="monitoring"?<Monitoring data={data} range={range} {...live} now={now}/>:view==="planner"?<Planner data={data} now={now} transportStale={transportStale} demo={demo}/>:view==="guard"?<Guard snapshot={snapshot} data={data} {...live} now={now}/>:view==="executor"?<Executor snapshot={snapshot} data={data}/>:<Verification data={data}/>}</>
}
function RecoveryStats({data,demo,transportStale,now}:{data:Scoped}&Live) {
  const latest=(kind:string)=>data.messages.find(r=>r.message.kind===kind)?.message
  const open=data.incidents.filter(i=>!["RECOVERED","REJECTED","FAILED"].includes(i.state)).length
  const active=data.actions.filter(a=>!["RECOVERED","REJECTED","FAILED"].includes(a.state)).length
  const cells:[string,ReactNode,string,string][]=[["Open / unresolved",<span className={cn("tile-value",open>0&&"text-bad")}>{open}</span>,`${data.incidents.length} incident records`,open>0?"var(--bad)":"var(--good)"],["Latest Guard decision",<GuardDecisionStatus decision={latest("guard_result")} demo={demo} transportStale={transportStale} now={now}/>,latest("guard_result")?.policy_version||"No decision received","var(--primary)"],["Recorded outcome",<Status value={latest("outcome")?.status}/>,latest("outcome")?.window_end?`Window ended ${age(latest("outcome")?.window_end)} · not a current probe`:"A stored result, not a current probe","var(--primary)"],["Active actions",<span className="tile-value">{active}</span>,"Scoped to current filters",active>0?"var(--warn)":"var(--primary)"]]
  return <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{cells.map(([label,value,detail,tone])=><div key={label} className="tile" style={{"--tone":tone} as CSSProperties}><p className="console-label">{label}</p><div className="mt-5 flex min-h-10 items-center">{value}</div><p className="mt-4 text-[11px] leading-relaxed text-muted-foreground">{detail}</p></div>)}</div>
}
function IncidentTable({data,inspect}:{data:Scoped;inspect:(id:string)=>void}){return <Panel title="Incident register" description="Open an incident to follow its evidence, proposal, Guard decision, execution and outcome together"><Records headers={["INCIDENT","WORKLOAD","STATE","ACTION","DETAILS"]} rows={data.incidents.map(i=>[<span className="font-mono text-[10px]">{i.incident_id}</span>,targetLabel(i.target),<Status value={i.state}/>,<span className="font-mono text-[10px]">{i.active_action_id||"Not proposed"}</span>,<Button variant="outline" size="sm" className="h-7 border-border bg-transparent text-[11px] dark:bg-transparent" onClick={()=>inspect(i.incident_id)} aria-label={`Open incident ${i.incident_id}`}><PanelRightOpen className="size-3.5"/>Open</Button>])}/></Panel>}
function latestObservations(data:Scoped){const found=new Map<string,Scoped["telemetry"][number]>();data.telemetry.filter(e=>e.event_type==="observation"&&e.target).forEach(e=>{if(!found.has(identity(e.target)))found.set(identity(e.target),e)});return [...found.values()]}
function Workloads({data,transportStale,demo,now}:{data:Scoped}&Live){return <Panel title="Observed workloads" description="Stale observations do not establish current health"><Records headers={["WORKLOAD","READY / DESIRED","AVAILABLE","OBSERVATION"]} rows={latestObservations(data).map(e=>{const current=demo||(!transportStale&&now-new Date(e.at).getTime()<=15000&&fresh(e.at));return [targetLabel(e.target),<span className={cn("font-mono tabular-nums",!current&&"text-muted-foreground line-through decoration-muted-foreground/40")}>{`${e.metrics.ready} / ${e.metrics.desired}`}</span>,e.metrics.available,<div><Status value={demo?"PLAYBACK":current?"RECENT":"STALE"}/><p className="mt-1 text-[10px] text-muted-foreground">{age(e.at)}</p></div>]})}/></Panel>}
function Coordinator({snapshot,data,now,inspect}:{snapshot:Snapshot;data:Scoped;now:number;inspect:(id:string)=>void}){const synthetic=snapshot.mode==="synthetic";return <><Context>Pending routes need an active consumer. Opening this dashboard does not process events or approve recovery.</Context><IncidentTable data={data} inspect={inspect}/><Panel title="Durable routing"><Records headers={["EVENT","TOPIC","ACTION","DELIVERY"]} rows={snapshot.outbox.filter(r=>!r.action_id||data.actionIds.has(r.action_id)).map(r=>[r.id,<span className="font-mono text-[11px]">{r.topic}</span>,r.action_id||"Incident / unscoped",<Status value={r.delivered?"ACKNOWLEDGED":"PENDING"}/>])}/></Panel><Panel title="Recorded approvals"><Records headers={["ACTION","OPERATOR","DECISION","APPROVED AT","EXPIRES"]} rows={snapshot.approvals.filter(r=>data.actionIds.has(r.record.action_id)).map(({record:r})=>[r.action_id,r.protected?"Protected approval":r.operator_id,<Status value={r.decision}/>,r.protected?"Protected metadata":time(r.approved_at),r.protected?"Use authenticated authority":r.expires_at?<div className="flex flex-wrap items-center gap-2"><span>{time(r.expires_at)}</span>{!synthetic&&new Date(r.expires_at).getTime()<=now&&<Status value="EXPIRED"/>}</div>:"No expiry recorded"])}/></Panel><EventFeed data={data} member={1}/></>}
function Monitoring({data,range,demo,transportStale,now}:{data:Scoped;range:ChartRange}&Live) {
  const latest=latestObservations(data)[0]
  const observations=latest?data.telemetry.filter(e=>e.event_type==="observation"&&identity(e.target)===identity(latest.target)).slice(0,400).reverse().map(e=>({time:time(e.at),capturedAt:e.at,ready:e.metrics.ready,desired:e.metrics.desired})):[]
  const signals=data.messages.filter(r=>r.message.kind==="incident").flatMap(r=>(r.message.signals||[]).map(s=>({...s,incident:r.message.incident_id})))
  return <><Context>Replica readiness and persistent-failure evidence from the Observer. HTTP error rates and distributed traces need separate application instrumentation.</Context><ReadinessChart data={observations} range={range} demo={demo} telemetry={data.telemetry} target={latest?.target}/><Workloads data={data} demo={demo} transportStale={transportStale} now={now}/><Panel title="Detection evidence"><Records headers={["INCIDENT","SIGNAL","SOURCE","DETAIL","AT"]} rows={signals.map(s=>[s.incident,<span className="font-medium">{s.reason}</span>,s.source,<Prose>{s.detail}</Prose>,time(s.at)])}/></Panel><EventFeed data={data} member={2}/></>
}
function PlanningProgress({data,now,transportStale,demo}:{data:Scoped}&Live) {
  const updates=data.telemetry.filter(e=>e.component===3&&e.progress)
  const latest=updates[0]
  const pending=data.incidents.filter(i=>i.state==="OPEN")
  const recent=latest&&(demo||(!transportStale&&now-new Date(latest.at).getTime()<=15000&&fresh(latest.at)))
  const active=latest&&["started","running","retrying"].includes(latest.progress!.status)
  return <Panel title="Planning activity" description="Model requests report elapsed time every five seconds. A heartbeat confirms the request is pending; it does not measure inference progress.">
    <CardContent className="space-y-4 p-5">
      <div role="status" aria-live="polite">
        <Status value={!latest?"NO DATA":active&&!recent?"STALE":latest.progress!.status==="failed"?"FAILED":active?"RUNNING":"RECORDED"}/>
        <p className="mt-2 text-sm">{latest?.summary||"No planning progress received. Start a Planner with progress reporting."}</p>
        {latest&&<p className="mt-1 text-xs text-muted-foreground">Last update {age(latest.at)}{latest.progress?.model?` ? ${latest.progress.model}`:""}{latest.progress?.attempt?` ? attempt ${latest.progress.attempt}`:""}</p>}
      </div>
      {pending.length>0&&<p className="text-xs text-muted-foreground">{pending.length} incident(s) awaiting a proposal. Incidents for a workload already recovering remain queued.</p>}
    </CardContent>
    <Records headers={["PHASE","STATUS","ELAPSED","TOKENS IN / OUT","UPDATED"]} rows={updates.filter(e=>e.event_type!=="heartbeat").slice(0,12).map(e=>[e.progress!.role||e.progress!.stage,e.progress!.status,e.progress!.seconds===undefined?"?":`${Math.round(e.progress!.seconds)}s`,e.progress!.prompt_tokens===undefined?"Pending":`${e.progress!.prompt_tokens} / ${e.progress!.output_tokens??0}`,time(e.at)])}/>
  </Panel>
}
function Planner({data,now,transportStale,demo}:{data:Scoped}&Live) {
  const proposals=data.messages.filter(r=>r.message.kind==="proposal")
  return <><Context>Recovery proposals come from the configured planning source. Reasoning and evidence references are producer claims, not independently verified diagnoses.</Context><PlanningProgress data={data} now={now} transportStale={transportStale} demo={demo}/>{proposals.length?proposals.map(({sequence,message:p})=><Panel key={sequence} title={`Proposal · ${p.action_id}`}><CardContent className="space-y-5 p-5"><p className="font-display text-2xl italic leading-snug text-foreground/95">{p.rationale||"No rationale recorded."}</p><Fields entries={[["Incident",p.incident_id],["Target",targetLabel(p.target)],["Action",<Status value={p.action}/>],["Based on version",p.based_on_resource_version],["Evidence references",p.evidence_refs?.join(", ")]]}/><Json value={p.parameters} label="Recovery parameters"/></CardContent></Panel>):<Panel title="Recovery proposals"><Records headers={[]} rows={[]}/></Panel>}<EventFeed data={data} member={3}/></>
}
function Guard({snapshot,data,demo,transportStale,now}:{snapshot:Snapshot;data:Scoped}&Live) {
  const decisions=data.messages.filter(r=>r.message.kind==="guard_result").map(r=>r.message),latest=decisions[0]
  const report=latest&&snapshot.reports.find(r=>r.action_id===latest.action_id&&r.checked_at===latest.checked_at&&r.inputs_sha256===latest.inputs_sha256&&r.proposal_sha256===latest.proposal_sha256)
  return <><Context>ALLOW is a recorded, expiring decision. The Executor must reload authoritative inputs and obtain fresh validation before writing. This dashboard grants no permission.</Context><Panel title="Decision history"><Records headers={["ACTION","VERDICT","REASONS","CHECKED AT","VALID UNTIL"]} rows={decisions.map(d=>[d.action_id,<GuardDecisionStatus decision={d} demo={demo} transportStale={transportStale} now={now}/>,<Prose>{d.reason_codes?.join(" · ")}</Prose>,time(d.checked_at),d.valid_until?<span className={cn(!demo&&new Date(d.valid_until).getTime()<=now&&"text-muted-foreground line-through decoration-muted-foreground/40")}>{time(d.valid_until)}</span>:"Legacy result"])}/></Panel>{latest&&<Panel title="Latest decision binding"><CardContent className="p-5"><Fields entries={[["Proposal SHA-256",<code className="text-[11px] text-primary/90">{latest.proposal_sha256||"Legacy result"}</code>],["Inputs SHA-256",<code className="text-[11px] text-primary/90">{latest.inputs_sha256||"Legacy result"}</code>],["Policy version",latest.policy_version],["Resource version",latest.validated_resource_version],["Time status",<div className="space-y-2"><GuardDecisionStatus decision={latest} demo={demo} transportStale={transportStale} now={now}/><p className="text-muted-foreground">{decisionPresentation(latest,{demo,transportStale,now}).detail}</p></div>]]}/></CardContent></Panel>}<Panel title="Individual policy findings"><Records headers={["RESULT","RULE","INPUT","EXPLANATION"]} rows={(report?.checks||[]).map(c=>[<Status value={c.status}/>,<code className="text-[10px]">{c.code}</code>,<code className="text-[10px] text-muted-foreground">{c.path}</code>,<Prose>{c.message}</Prose>])}/>{!report&&<p className="px-5 pb-4 text-xs text-muted-foreground">No matching detailed report. Historical or externally ingested decisions may not include one.</p>}</Panel>{report&&<Json value={report} label="Inspect bound report and actual / expected values"/>}<EventFeed data={data} member={4}/></>
}
function Executor({snapshot,data}:{snapshot:Snapshot;data:Scoped}) {
  const results=data.messages.filter(r=>r.message.kind==="execution_result").map(r=>r.message)
  return <><Context>The live Executor uses restricted Kubernetes credentials and a durable journal. A successful write routes to the Verifier before the incident is closed.</Context><Panel title="Recorded execution attempts"><Records headers={["ACTION","RESULT","RESOURCE VERSION","RECORDED AT","DETAIL"]} rows={results.map(r=>[r.action_id,<Status value={r.status}/>,<span className="font-mono">{r.resource_version||"Not recorded"}</span>,time(r.recorded_at),<Prose>{r.details}</Prose>])}/></Panel><Panel title="Executor routing"><Records headers={["EVENT","TOPIC","ACTION","DELIVERY"]} rows={snapshot.outbox.filter(r=>r.action_id&&data.actionIds.has(r.action_id)&&r.topic.startsWith("executor.")).map(r=>[r.id,<span className="font-mono text-[11px]">{r.topic}</span>,r.action_id,<Status value={r.delivered?"ACKNOWLEDGED":"PENDING"}/>])}/></Panel><EventFeed data={data} member={5}/></>
}
function Verification({data}:{data:Scoped}) {
  const dependencies=data.messages.filter(r=>r.message.kind==="dependency_result").map(r=>r.message),outcomes=data.messages.filter(r=>r.message.kind==="outcome").map(r=>r.message)
  return <><Context>Role 6 evaluates the reviewed dependency graph before execution and real application evidence afterward. Boutique uses gRPC quote, readiness and template checks. Missing results remain unknown.</Context><Panel title="Pre-execution dependency findings"><Records headers={["ACTION","STATUS","AFFECTED SERVICES","BLOCKERS / UNKNOWNS","CHECKED AT"]} rows={dependencies.map(d=>[d.action_id,<Status value={d.status}/>,d.affected_services?.join(", ")||"None listed",<Prose>{[...(d.blockers||[]),...(d.unknowns||[])].join(" · ")||"None recorded"}</Prose>,time(d.checked_at)])}/></Panel>{outcomes.length?outcomes.map(o=><Panel key={`${o.action_id}-${o.window_end}`} title={`Post-execution outcome · ${o.action_id}`}><CardContent className="p-5"><Fields entries={[["Outcome",<Status value={o.status}/>],["Window start",time(o.window_start)],["Window end",time(o.window_end)],["Window duration",o.window_start&&o.window_end?`${(new Date(o.window_end).getTime()-new Date(o.window_start).getTime())/1000} seconds`:"Unknown"]]}/></CardContent><Records headers={["CHECK","RESULT","DETAIL"]} rows={(o.checks||[]).map(c=>[<span className="font-mono text-[11px]">{c.name}</span>,<Status value={c.passed?"PASS":"FAIL"}/>,<Prose>{c.detail}</Prose>])}/></Panel>):<Panel title="Post-execution health"><Records headers={[]} rows={[]}/></Panel>}<EventFeed data={data} member={6}/></>
}
