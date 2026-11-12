import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { ApprovalPanel } from "./approval-panel"
import type { Action, ApprovalRequest } from "@/lib/types"

const NOW = Date.parse("2026-11-12T10:00:00Z")
const DIGEST = "d".repeat(64)
const target = {cluster_id: "sentinel-boutique-lab", namespace: "sentinel-boutique", kind: "Deployment",
  name: "shippingservice", uid: "uid-1"}

function request(changes: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {action_id: "ACT-LIVE-1", incident_id: "INC-1", status: "PENDING", mode: "ui", operator_id: "dashboard-operator",
    target, action: "ROLLBACK", baseline_id: "BASE-LIVE-1", template_sha256_prefix: "a9091159ce70",
    proposal_sha256: DIGEST, policy_version: "executor-live-lab-1",
    guard: {verdict: "ESCALATE", reason_codes: ["OPERATOR_APPROVAL_REQUIRED"]},
    rationale: "Restore the measured healthy template", rationale_truncated: false,
    requested_at: "2026-11-12T09:59:00Z", expires_at: "2026-11-12T10:09:00Z",
    approval: null, revocation: null, ...changes}
}
function action(state: string): Action {
  return {action_id: "ACT-LIVE-1", incident_id: "INC-1", state, guard_result: null, execution_result: null,
    proposal: {schema_version: "1.0", kind: "proposal", incident_id: "INC-1", action_id: "ACT-LIVE-1", target}}
}
const approved = {approval_id: 4, operator_id: "dashboard-operator", approved_at: "2026-11-12T09:59:30Z",
  expires_at: "2026-11-12T10:02:30Z"}
const refresh = vi.fn()

function show(requests = [request()], actions = [action("AWAITING_APPROVAL")], options: {stale?: boolean; paused?: boolean} = {}) {
  return render(<ApprovalPanel requests={requests} actions={actions} sessionId="session-1" stale={options.stale ?? false}
    paused={options.paused ?? false} now={NOW} refresh={refresh}/>)
}
function relay(...answers: {ok: boolean; status: number; body: unknown}[]) {
  const fetcher = vi.fn().mockResolvedValueOnce({ok: true, json: async () => ({session_id: "session-1", control_token: "lab-secret"})})
  for (const answer of answers) fetcher.mockResolvedValueOnce({ok: answer.ok, status: answer.status, json: async () => answer.body})
  vi.stubGlobal("fetch", fetcher)
  return fetcher
}
beforeEach(() => {vi.clearAllMocks(); vi.unstubAllGlobals()})

describe("dashboard approval panel", () => {
  it("renders the pending action's code-derived facts and the untrusted rationale label", () => {
    show()
    expect(screen.getByText("Waiting for an operator decision")).toBeInTheDocument()
    for (const fact of ["ACT-LIVE-1", "ROLLBACK", "BASE-LIVE-1", "a9091159ce70", DIGEST, "uid-1",
      "ESCALATE: OPERATOR_APPROVAL_REQUIRED", "dashboard-operator (mode ui)"]) {
      expect(screen.getByText(fact)).toBeInTheDocument()
    }
    expect(screen.getByText("sentinel-boutique-lab/sentinel-boutique Deployment/shippingservice")).toBeInTheDocument()
    expect(screen.getByText(/2026-11-12T10:09:00Z \(540s left\)/)).toBeInTheDocument()
    expect(screen.getByText("Model-written rationale (untrusted)")).toBeInTheDocument()
    expect(screen.queryByRole("button", {name: "Revoke approval"})).not.toBeInTheDocument()
  })

  it("renders nothing without requests and offers no approval unless the Guard asked for one", () => {
    const empty = show([])
    expect(empty.container).toBeEmptyDOMElement()
    empty.unmount()
    show([request({guard: {verdict: "ESCALATE", reason_codes: ["DEPENDENCY_UNKNOWN"]}})])
    expect(screen.queryByRole("button", {name: "Approve"})).not.toBeInTheDocument()
  })

  it("explains a RESTART as new Pods from the unchanged template", () => {
    show([request({action: "RESTART"})])
    expect(screen.getByText("RESTART")).toBeInTheDocument()
    expect(screen.getByText(/replaces every Pod\. No configuration change\./)).toBeInTheDocument()
  })

  it("keeps the model's text literal, so a script payload is shown and never run", () => {
    const payload = "<script>window.__ran = true</script><img src=x onerror=alert(1)>"
    const view = show([request({rationale: payload, rationale_truncated: true})])
    expect(screen.getByText(`${payload} [truncated]`)).toBeInTheDocument()
    expect(view.container.querySelector("script, img")).toBeNull()
    expect((window as unknown as {__ran?: boolean}).__ran).toBeUndefined()
  })

  it("requires the exact action ID typed before Approve is enabled", async () => {
    show()
    const approve = screen.getByRole("button", {name: "Approve"})
    expect(approve).toBeDisabled()
    const field = screen.getByLabelText("Type the action ID to approve")
    expect(field).toHaveAttribute("autocomplete", "off")
    for (const wrong of ["ACT-LIVE", "act-live-1", " ACT-LIVE-1", "ACT-LIVE-1 "]) {
      await userEvent.clear(field)
      await userEvent.type(field, wrong)
      expect(approve).toBeDisabled()
    }
    await userEvent.clear(field)
    await userEvent.type(field, "ACT-LIVE-1")
    expect(approve).toBeEnabled()
  })

  it("posts only the lab token and the snapshot's digest, then disables after the decision", async () => {
    const fetcher = relay({ok: true, status: 200, body: {action_id: "ACT-LIVE-1", ...approved, mode: "ui"}})
    show()
    await userEvent.type(screen.getByLabelText("Type the action ID to approve"), "ACT-LIVE-1")
    await userEvent.click(screen.getByRole("button", {name: "Approve"}))
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce())
    expect(fetcher).toHaveBeenCalledTimes(2)
    const [path, options] = fetcher.mock.calls[1]
    expect(path).toBe("/api/approvals/approve")
    expect(JSON.parse(options.body)).toEqual({action_id: "ACT-LIVE-1", proposal_sha256: DIGEST, confirm_action_id: "ACT-LIVE-1"})
    expect(options.headers).toEqual({"Content-Type": "application/json", "X-Sentinel-Lab-Token": "lab-secret"})
    expect(screen.getByRole("status")).toHaveTextContent("Approval recorded")
    expect(screen.queryByRole("button", {name: "Approve"})).not.toBeInTheDocument()
    expect(document.body.textContent).not.toContain("lab-secret")
  })

  it("shows the relay's refusal and keeps the request open", async () => {
    relay({ok: false, status: 400, body: {error: "The typed action ID does not match this action."}})
    show()
    await userEvent.type(screen.getByLabelText("Type the action ID to approve"), "ACT-LIVE-1")
    await userEvent.click(screen.getByRole("button", {name: "Approve"}))
    expect(await screen.findByRole("alert")).toHaveTextContent("does not match")
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
    expect(refresh).not.toHaveBeenCalled()
    expect(screen.getByRole("button", {name: "Approve"})).toBeInTheDocument()
  })

  it("refuses to post when the lab session changed", async () => {
    const fetcher = vi.fn().mockResolvedValue({ok: true, json: async () => ({session_id: "other", control_token: "x"})})
    vi.stubGlobal("fetch", fetcher)
    show()
    await userEvent.type(screen.getByLabelText("Type the action ID to approve"), "ACT-LIVE-1")
    await userEvent.click(screen.getByRole("button", {name: "Approve"}))
    expect(await screen.findByRole("alert")).toHaveTextContent("session changed")
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it("disables decisions while the connection is lost or updates are paused, and after expiry", () => {
    const view = show([request()], [action("AWAITING_APPROVAL")], {stale: true})
    expect(screen.getByLabelText("Type the action ID to approve")).toBeDisabled()
    view.unmount()
    const paused = show([request()], [action("AWAITING_APPROVAL")], {paused: true})
    expect(screen.getByRole("button", {name: "Approve"})).toBeDisabled()
    paused.unmount()
    show([request({expires_at: "2026-11-12T09:59:59Z"})])
    expect(screen.queryByRole("button", {name: "Approve"})).not.toBeInTheDocument()
  })

  it("shows Revoke for an approval until execution is reported and sends the latest approval ID", async () => {
    const fetcher = relay({ok: true, status: 200, body: {action_id: "ACT-LIVE-1", approval_id: 4, mode: "ui",
      operator_id: "dashboard-operator", revoked_at: "2026-11-12T10:00:01Z"}})
    const view = show([request({status: "APPROVED", approval: approved})], [action("READY")])
    expect(screen.getByText("Approved through the approval authority")).toBeInTheDocument()
    expect(screen.queryByRole("button", {name: "Approve"})).not.toBeInTheDocument()
    const revoke = screen.getByRole("button", {name: "Revoke approval"})
    expect(revoke).toBeDisabled()
    await userEvent.type(screen.getByLabelText("Type the action ID to revoke the approval"), "ACT-LIVE-1")
    await userEvent.click(revoke)
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce())
    const [path, options] = fetcher.mock.calls[1]
    expect(path).toBe("/api/approvals/revoke")
    expect(JSON.parse(options.body)).toEqual({action_id: "ACT-LIVE-1", proposal_sha256: DIGEST,
      confirm_action_id: "ACT-LIVE-1", approval_id: 4})
    expect(screen.getByRole("status")).toHaveTextContent("Revocation recorded")
    expect(screen.queryByRole("button", {name: "Revoke approval"})).not.toBeInTheDocument()
    view.unmount()
    for (const state of ["EXECUTING", "VERIFYING", "RECOVERED"]) {
      const later = show([request({status: "APPROVED", approval: approved})], [action(state)])
      expect(screen.queryByRole("button", {name: "Revoke approval"})).not.toBeInTheDocument()
      later.unmount()
    }
    show([request({status: "REVOKED", approval: approved,
      revocation: {operator_id: "dashboard-operator", revoked_at: "2026-11-12T10:00:01Z"}})], [action("VALIDATING")])
    expect(screen.getByText(/Approval revoked/)).toBeInTheDocument()
    expect(screen.queryByRole("button", {name: "Revoke approval"})).not.toBeInTheDocument()
  })
})
