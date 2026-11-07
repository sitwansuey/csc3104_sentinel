import { useEffect, useState } from "react"
import { ArrowRight, CheckCircle2, FlaskConical, LoaderCircle, ShieldCheck, Timer } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import type { LabSession } from "@/lib/types"
import { cn } from "@/lib/utils"

const buttonLabels: Record<string, string> = {
  primary: "Inject invalid PORT",
  crash: "Inject fault + crash Executor",
  "unnecessary-rollback": "Inject false recommendation",
}
const terminal = new Set(["COMPLETED", "FAILED", "STOPPED"])

export function LabExperiment({lab, stale, paused, now, refresh, inspect}: {
  lab: LabSession; stale: boolean; paused: boolean; now: number;
  refresh: () => void; inspect: (id: string) => void;
}) {
  const [posting, setPosting] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { setMessage(null); setError(null) }, [lab.gate_id])
  const active = lab.scenarios.find(item => item.id === lab.active_scenario)
  const passed = lab.scenarios.filter(item => item.status === item.expected).length
  const disabled = posting || stale || paused || !lab.can_inject || !lab.gate_id

  async function inject() {
    if (disabled) return
    setPosting(true); setError(null); setMessage(null)
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 5000)
    try {
      const capabilities = await fetch("/api/lab", {cache: "no-store", signal: controller.signal})
      if (!capabilities.ok) throw new Error("Lab controls are unavailable. Refresh the session.")
      const access = await capabilities.json() as {session_id: string; control_token: string}
      if (access.session_id !== lab.session_id) throw new Error("The experiment session changed. Refresh before injecting.")
      const response = await fetch("/api/lab/inject", {
        method: "POST", signal: controller.signal,
        headers: {"Content-Type": "application/json", "X-Sentinel-Lab-Token": access.control_token},
        body: JSON.stringify({session_id: lab.session_id, gate_id: lab.gate_id}),
      })
      if (!response.ok) throw new Error(response.status === 409
        ? "The scenario changed. Refresh before injecting."
        : "Injection request was refused. Refresh the session before retrying.")
      setMessage("Injection requested. Follow the measured recovery evidence below.")
      refresh()
    } catch (failure) {
      setError(failure instanceof Error && failure.name !== "AbortError" ? failure.message
        : "The response was lost. Refresh the scenario state before retrying.")
    } finally {
      clearTimeout(timeout); setPosting(false)
    }
  }

  return <section aria-label="Boutique experiment controls" className="rise space-y-4">
    <div className="lab">
      <div className="relative space-y-6 p-5 sm:p-7">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-2xl">
            <p className="eyebrow bare text-warn"><FlaskConical className="size-4"/>LIVE BOUTIQUE LAB</p>
            <h2 className="mt-4 font-display text-4xl italic leading-none tracking-tight sm:text-5xl">Inject a fault. Follow the recovery.</h2>
            <p className="mt-3 text-sm leading-relaxed text-muted-foreground">Run one controlled scenario at a time against {lab.target.name}. The existing Guard, restricted Executor and Verifier process the result.</p>
          </div>
          <span className="chip" data-tone={lab.state === "FAILED" ? "bad" : lab.state === "COMPLETED" ? "good" : "warn"}>{lab.state.replaceAll("_", " ")}</span>
        </div>
        <div className="flex flex-wrap gap-x-7 gap-y-2 font-mono text-[11px] text-muted-foreground">
          <span>Cluster <strong className="ml-1 font-medium text-foreground">{lab.target.cluster_id}</strong></span>
          <span>Namespace <strong className="ml-1 font-medium text-foreground">{lab.target.namespace}</strong></span>
          <span>Expected outcomes reached <strong className="ml-1 font-medium text-foreground">{passed} / {lab.scenarios.length}</strong></span>
        </div>
        <div className="rounded-xl border border-border bg-background/70 p-4 sm:flex sm:items-center sm:justify-between sm:gap-5">
          <div aria-live="polite">
            <p className="text-sm font-semibold">{lab.state === "PREPARING" ? "Measuring the healthy baseline"
              : lab.state === "COMPLETED" ? "All scenarios reached their expected outcomes"
              : lab.state === "FAILED" ? "Experiment failed — inspect the recorded evidence"
              : lab.state === "STOPPED" ? "Experiment stopped"
              : active ? active.title : "Preparing the next scenario"}</p>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{stale ? "Connection lost. Controls are disabled until fresh state arrives."
              : paused ? "Resume dashboard updates before injecting."
              : lab.state === "PREPARING" ? "Controls become available after baseline capture and permission checks."
              : lab.state === "RUNNING" ? "Scenario in progress. No second injection can start."
              : terminal.has(lab.state) ? "Inspect incident details below. Start a fresh lab session to repeat the experiment."
              : active?.description}</p>
          </div>
          <Button className="mt-4 min-h-10 shrink-0 bg-warn text-warn-foreground shadow-[0_0_0_1px_var(--warn),0_10px_30px_-12px_var(--warn)] hover:bg-warn/90 sm:mt-0" disabled={disabled} onClick={() => void inject()}>
            {posting || lab.state === "RUNNING" ? <LoaderCircle className="size-4 animate-spin"/> : <FlaskConical className="size-4"/>}
            {posting ? "Requesting injection…" : lab.state === "COMPLETED" ? "Experiment complete"
              : lab.state === "FAILED" ? "Experiment failed" : lab.state === "STOPPED" ? "Experiment stopped"
              : lab.state === "RUNNING" ? "Scenario running" : buttonLabels[active?.id || ""] || "Waiting for baseline"}
          </Button>
        </div>
        {message && !error && <p role="status" className="font-mono text-xs text-primary">{message}</p>}
        {error && <p role="alert" className="text-sm text-bad">{error}</p>}
        <div className="grid gap-3 lg:grid-cols-3">
          {lab.scenarios.map((item, index) => {
            const finished = item.status === item.expected
            const end = item.finished_at ? Date.parse(item.finished_at) : now
            const elapsed = item.requested_at ? Math.max(0, Math.floor((end - Date.parse(item.requested_at)) / 1000)) : null
            return <article key={item.id} data-ready={item.status === "READY" || undefined} className="lab-scenario">
              <div className="flex items-center justify-between gap-2"><span className="font-mono text-[10px] tracking-[.18em] text-muted-foreground">SCENARIO 0{index + 1}</span>
                <Badge variant="outline" className={cn("rounded font-mono text-[9px] tracking-wide", finished && "border-good/40 text-good")}>{finished && <CheckCircle2 className="mr-1 size-3"/>}{item.status}</Badge></div>
              <h3 className="mt-3 font-display text-xl italic leading-tight">{item.title}</h3>
              <p className="mt-2 min-h-12 text-xs leading-relaxed text-muted-foreground">{item.description}</p>
              <p className="mt-3 flex items-center gap-1.5 font-mono text-[11px] text-muted-foreground"><Timer className="size-3"/>{elapsed === null ? "Not requested" : `${elapsed}s since request${item.finished_at ? " to outcome" : ""}`}</p>
              {item.incident_id && <Button variant="ghost" size="sm" className="mt-2 h-8 px-0 text-xs text-primary hover:bg-transparent" onClick={() => inspect(item.incident_id!)}>Inspect incident<ArrowRight className="size-3"/></Button>}
            </article>
          })}
        </div>
        <p className="flex items-start gap-2 text-[11px] leading-relaxed text-muted-foreground"><ShieldCheck className="mt-.5 size-4 shrink-0 text-warn"/>Harness-authored proposals and demo-recorded approval. Guard constraints still apply. APPLIED is execution evidence; only a passing Verifier outcome establishes recovery.</p>
      </div>
    </div>
  </section>
}
