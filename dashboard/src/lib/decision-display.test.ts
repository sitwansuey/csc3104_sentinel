import { describe, expect, it } from "vitest"
import { decisionPresentation } from "./decision-display"
import type { WireMessage } from "./types"

const now = Date.parse("2026-10-25T12:00:10Z")
const decision: WireMessage = {
  kind: "guard_result", schema_version: "2.0", incident_id: "INC-1", action_id: "ACT-1",
  target: { cluster_id: "lab", namespace: "boutique", kind: "Deployment", name: "cart", uid: "uid-1" },
  verdict: "ALLOW", reason_codes: ["POLICY_SATISFIED"],
  checked_at: "2026-10-25T12:00:00Z", valid_until: "2026-10-25T12:00:30Z",
}

describe("recorded Guard decision presentation", () => {
  it("labels an unexpired ALLOW as a record and explains the Executor recheck", () => {
    const result = decisionPresentation(decision, { now })
    expect(result.label).toBe("ALLOW · RECORDED")
    expect(result.tone).toBe("good")
    expect(result.detail).toContain("Executor must still obtain fresh state")
    expect(result.detail).toContain("not execution permission")
  })

  it("expires precisely at valid_until, including when a paused view's clock advances", () => {
    const expiry = Date.parse(decision.valid_until!)
    expect(decisionPresentation(decision, { now: expiry - 1 }).tone).toBe("good")
    expect(decisionPresentation(decision, { now: expiry })).toMatchObject({ label: "ALLOW · EXPIRED", tone: "warning" })
    expect(decisionPresentation(decision, { now: expiry + 60_000 }).tone).not.toBe("good")
    expect(decision.valid_until).toBe("2026-10-25T12:00:30Z")
  })

  it.each([
    undefined, "", "garbage", "2026-10-25T12:00:11Z", "2026-02-30T12:00:00Z",
    "2026-10-25T12:00:00", "2026-10-25T24:00:00Z", "2026-13-01T12:00:00Z",
  ])("never presents missing, future or malformed checked_at %s as green", checked_at => {
    expect(decisionPresentation({ ...decision, checked_at }, { now })).toMatchObject({ label: "ALLOW · INVALID TIME", tone: "warning" })
  })

  it.each([undefined, ""])("identifies a missing expiry %s as a legacy or incomplete record", valid_until => {
    const result = decisionPresentation({ ...decision, valid_until }, { now })
    expect(result).toMatchObject({ label: "ALLOW · NO EXPIRY", tone: "warning" })
    expect(result.detail).toContain("cannot establish current execution permission")
  })

  it.each([
    "not a date", "2026-10-25T12:00:00Z", "2026-10-25T11:59:59Z", "2026-02-30T12:00:00Z",
  ])("never presents an invalid decision window ending %s as green", valid_until => {
    expect(decisionPresentation({ ...decision, valid_until }, { now })).toMatchObject({ label: "ALLOW · INVALID WINDOW", tone: "warning" })
  })

  it("treats a stale dashboard connection as uncertainty even before expiry", () => {
    const result = decisionPresentation(decision, { now, transportStale: true })
    expect(result).toMatchObject({ label: "ALLOW · STALE VIEW", tone: "warning" })
    expect(result.detail).toContain("must not be treated as current execution permission")
  })

  it("keeps demo ALLOW neutral regardless of the fixture clock", () => {
    const result = decisionPresentation(decision, { now: now + 86_400_000, demo: true })
    expect(result).toMatchObject({ label: "ALLOW · DEMO", tone: "neutral" })
    expect(result.detail).toContain("fixture clock")
    expect(result.detail).toContain("does not authorize live execution")
  })

  it.each(["REJECT", "ESCALATE"])("preserves the recorded %s and reasons even when old or disconnected", verdict => {
    const result = decisionPresentation({ ...decision, verdict, reason_codes: ["STATE_CHANGED", "DEPENDENCY_UNKNOWN"] }, { now: now + 86_400_000, transportStale: true })
    expect(result.label).toBe(`${verdict} · RECORDED`)
    expect(result.detail).toContain("STATE_CHANGED, DEPENDENCY_UNKNOWN")
    expect(result.detail).toContain("newer decision may exist")
    expect(result.tone).not.toBe("good")
  })

  it("accepts an explicit timezone offset representing the same validity window", () => {
    expect(decisionPresentation({ ...decision, checked_at: "2026-10-25T20:00:00+08:00", valid_until: "2026-10-25T20:00:30+08:00" }, { now }).label).toBe("ALLOW · RECORDED")
  })

  it("does not invent a decision for absent or unsupported input", () => {
    expect(decisionPresentation(undefined, { now })).toMatchObject({ label: "NO DECISION", tone: "neutral" })
    expect(decisionPresentation({ ...decision, verdict: "MAYBE" }, { now })).toMatchObject({ label: "UNKNOWN DECISION", tone: "warning" })
    expect(decisionPresentation(decision, { now: NaN }).tone).not.toBe("good")
  })
})
