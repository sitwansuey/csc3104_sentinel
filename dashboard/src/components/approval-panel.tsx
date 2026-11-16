import { useState } from "react"
import { CheckCircle2, LoaderCircle, ShieldAlert, ShieldCheck, Undo2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { actionEffect } from "@/lib/actions"
import type { Action, ApprovalRequest } from "@/lib/types"

/** States in which the approval authority still accepts a revocation (before execution is reported). */
const revocable = new Set(["VALIDATING", "AWAITING_APPROVAL", "READY"])
const statusText: Record<ApprovalRequest["status"], string> = {
  PENDING: "Waiting for an operator decision",
  DECIDING: "Sending the approval to the approval authority",
  APPROVED: "Approved through the approval authority",
  REVOKING: "Sending the revocation to the approval authority",
  REVOKED: "Approval revoked; the Guard must check the action again",
  EXPIRED: "Request expired; nothing was approved",
  CLOSED: "The harness stopped waiting; nothing was approved",
}

type Operation = "approve" | "revoke"

/**
 * Decides one escalated action through the dashboard server. The page sends only the
 * per-session lab token; the server relays the decision with its own operator credential.
 */
export function ApprovalPanel({requests, actions, sessionId, stale, paused, now, refresh}: {
  requests: ApprovalRequest[]; actions: Action[]; sessionId: string; stale: boolean; paused: boolean;
  now: number; refresh: () => void;
}) {
  if (!requests.length) return null
  return <section aria-label="Operator approval" className="rise space-y-3">
    {requests.map(request => <ApprovalCard key={`${request.action_id}:${request.requested_at}`} request={request}
      action={actions.find(item => item.action_id === request.action_id)} sessionId={sessionId}
      stale={stale} paused={paused} now={now} refresh={refresh}/>)}
  </section>
}

function ApprovalCard({request, action, sessionId, stale, paused, now, refresh}: {
  request: ApprovalRequest; action?: Action; sessionId: string; stale: boolean; paused: boolean;
  now: number; refresh: () => void;
}) {
  const [typed, setTyped] = useState("")
  const [posting, setPosting] = useState<Operation | null>(null)
  const [decided, setDecided] = useState<Operation[]>([])
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const state = action?.state
  const remaining = Math.max(0, Math.floor((Date.parse(request.expires_at) - now) / 1000))
  const approvalRemaining = request.approval ? Math.max(0, Math.floor((Date.parse(request.approval.expires_at) - now) / 1000)) : 0
  const confirmed = typed === request.action_id
  const blocked = posting !== null || stale || paused
  const canApprove = request.status === "PENDING" && remaining > 0 && !decided.includes("approve")
    && request.guard.reason_codes.includes("OPERATOR_APPROVAL_REQUIRED") && (state === undefined || state === "AWAITING_APPROVAL")
  const canRevoke = request.status === "APPROVED" && request.approval !== null && approvalRemaining > 0
    && !decided.includes("revoke") && state !== undefined && revocable.has(state)

  async function decide(operation: Operation) {
    if (blocked || !confirmed || (operation === "approve" ? !canApprove : !canRevoke)) return
    setPosting(operation); setError(null); setMessage(null)
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 15000)
    try {
      const capabilities = await fetch("/api/lab", {cache: "no-store", signal: controller.signal})
      if (!capabilities.ok) throw new Error("Lab controls are unavailable. Refresh the session.")
      const access = await capabilities.json() as {session_id: string; control_token: string}
      if (access.session_id !== sessionId) throw new Error("The experiment session changed. Refresh before deciding.")
      const body: Record<string, string | number> = {action_id: request.action_id,
        proposal_sha256: request.proposal_sha256, confirm_action_id: typed}
      if (operation === "revoke" && request.approval) body.approval_id = request.approval.approval_id
      const response = await fetch(`/api/approvals/${operation}`, {
        method: "POST", signal: controller.signal, cache: "no-store",
        headers: {"Content-Type": "application/json", "X-Sentinel-Lab-Token": access.control_token},
        body: JSON.stringify(body),
      })
      const answer = await response.json().catch(() => ({})) as {error?: unknown}
      if (!response.ok) throw new Error(typeof answer.error === "string" ? answer.error
        : `The dashboard refused the request (HTTP ${response.status}). Refresh before retrying.`)
      setDecided(previous => [...previous, operation]); setTyped("")
      setMessage(operation === "approve" ? "Approval recorded. The Guard checks the action again before any write."
        : "Revocation recorded. The action returns to the Guard; no write follows from this approval.")
      refresh()
    } catch (failure) {
      setError(failure instanceof Error && failure.name !== "AbortError" ? failure.message
        : "The response was lost. Refresh before retrying; the snapshot shows whether it was recorded.")
    } finally {
      clearTimeout(timeout); setPosting(null)
    }
  }

  const facts: [string, string][] = [
    ["Target", `${request.target.cluster_id}/${request.target.namespace} ${request.target.kind}/${request.target.name}`],
    ["Target UID", request.target.uid],
    ["Action", request.action],
    ["Effect", actionEffect(request.action)],
    ["Action ID", request.action_id],
    ["Baseline", request.baseline_id],
    ["Template SHA-256", request.template_sha256_prefix],
    ...(typeof request.desired_replicas === "number"
      ? [["Replica count", String(request.desired_replicas)] as [string, string]] : []),
    ["Proposal digest", request.proposal_sha256],
    ["Guard verdict", `${request.guard.verdict}: ${request.guard.reason_codes.join(", ") || "no reason codes"}`],
    ["Policy version", request.policy_version],
    ["Relayed as", `${request.operator_id} (mode ${request.mode})`],
  ]
  const active = canApprove || canRevoke
  return <article aria-label={`Approval for ${request.action_id}`} className="lab">
    <div className="relative space-y-4 p-5 sm:p-7">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-2xl">
          <p className="eyebrow bare text-warn"><ShieldAlert className="size-4"/>OPERATOR APPROVAL</p>
          <h2 className="mt-3 font-display text-3xl italic leading-tight tracking-tight">{statusText[request.status]}</h2>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">The facts below come from the stored proposal and the Guard decision. Approving creates a short-lived approval for this exact proposal; the Guard still checks every hard constraint, and the Executor reads the approval again just before its write.</p>
        </div>
        <Badge variant="outline" className="rounded font-mono text-[10px] tracking-wide">{request.status}</Badge>
      </div>
      <dl className="grid gap-x-6 gap-y-2 text-xs sm:grid-cols-[max-content_1fr]">
        {facts.map(([label, value]) => <div key={label} className="contents">
          <dt className="font-mono text-[11px] text-muted-foreground">{label}</dt>
          <dd className="break-all font-mono text-[11.5px]">{value}</dd>
        </div>)}
        <dt className="font-mono text-[11px] text-muted-foreground">{request.status === "PENDING" ? "Request expires" : "Requested"}</dt>
        <dd className="font-mono text-[11.5px]">{request.status === "PENDING" ? `${request.expires_at} (${remaining}s left)` : request.requested_at}</dd>
        {request.approval && <>
          <dt className="font-mono text-[11px] text-muted-foreground">Approval</dt>
          <dd className="font-mono text-[11.5px]">#{request.approval.approval_id} by {request.approval.operator_id} at {request.approval.approved_at}; expires {request.approval.expires_at}</dd>
        </>}
        {request.revocation && <>
          <dt className="font-mono text-[11px] text-muted-foreground">Revocation</dt>
          <dd className="font-mono text-[11.5px]">by {request.revocation.operator_id} at {request.revocation.revoked_at}</dd>
        </>}
        <dt className="font-mono text-[11px] text-muted-foreground">Action state</dt>
        <dd className="font-mono text-[11.5px]">{state || "not in the current snapshot"}</dd>
      </dl>
      <details className="rounded-lg border border-border bg-background/60 p-3 text-xs">
        <summary className="cursor-pointer font-medium">Model-written rationale (untrusted)</summary>
        <p className="mt-2 whitespace-pre-wrap break-words text-muted-foreground">{request.rationale || "No rationale recorded."}{request.rationale_truncated && " [truncated]"}</p>
      </details>
      {active && <div className="space-y-3 rounded-xl border border-border bg-background/70 p-4">
        <label className="block text-xs font-medium" htmlFor={`confirm-${request.action_id}`}>
          Type the action ID to {canApprove ? "approve" : "revoke the approval"}
        </label>
        <input id={`confirm-${request.action_id}`} value={typed} onChange={event => setTyped(event.target.value)}
          autoComplete="off" spellCheck={false} disabled={blocked}
          className="h-9 w-full rounded-md border border-input bg-transparent px-3 font-mono text-xs"/>
        <div className="flex flex-wrap gap-2">
          {canApprove && <Button disabled={blocked || !confirmed} onClick={() => void decide("approve")}>
            {posting === "approve" ? <LoaderCircle className="size-4 animate-spin"/> : <ShieldCheck className="size-4"/>}
            {posting === "approve" ? "Approving…" : "Approve"}
          </Button>}
          {canRevoke && <Button variant="destructive" disabled={blocked || !confirmed} onClick={() => void decide("revoke")}>
            {posting === "revoke" ? <LoaderCircle className="size-4 animate-spin"/> : <Undo2 className="size-4"/>}
            {posting === "revoke" ? "Revoking…" : "Revoke approval"}
          </Button>}
        </div>
        <p className="text-[11px] text-muted-foreground">{stale ? "Connection lost. Decisions are disabled until fresh state arrives."
          : paused ? "Resume dashboard updates before deciding."
          : canRevoke ? "Revocation is accepted until execution is reported. A revocation that arrives after the Executor's final approval lookup cannot stop that write."
          : "Anything other than the exact action ID keeps the button disabled."}</p>
      </div>}
      {message && !error && <p role="status" className="flex items-center gap-1.5 font-mono text-xs text-primary"><CheckCircle2 className="size-3.5"/>{message}</p>}
      {error && <p role="alert" className="text-sm text-bad">{error}</p>}
    </div>
  </article>
}
