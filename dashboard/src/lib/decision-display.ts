import type { WireMessage } from "./types"

export type DecisionTone = "good" | "danger" | "warning" | "neutral"
export interface DecisionPresentation {
  label: string
  detail: string
  tone: DecisionTone
}
export interface DecisionDisplayOptions {
  demo?: boolean
  transportStale?: boolean
  /** Browser clock in milliseconds. Supply a fixed clock for a reproducible view. */
  now?: number
}

// Require an explicit timezone and reject calendar dates that Date.parse would
// otherwise silently roll over (for example, February 30).
function timestamp(value?: string): number | null {
  if (typeof value !== "string") return null
  const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(value)
  if (!parts) return null
  const [, year, month, day, hour, minute, second] = parts.map(Number)
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  if (month < 1 || month > 12 || day < 1 || day > days[month - 1] || hour > 23 || minute > 59 || second > 59) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** Presentation of a stored decision, never an execution authorization check. */
export function decisionPresentation(
  decision: WireMessage | undefined,
  { demo = false, transportStale = false, now = Date.now() }: DecisionDisplayOptions = {},
): DecisionPresentation {
  if (!decision) return { label: "NO DECISION", detail: "No Guard decision has been recorded for this action.", tone: "neutral" }
  const verdict = decision.verdict
  const reasons = decision.reason_codes?.length ? ` Recorded reasons: ${decision.reason_codes.join(", ")}.` : ""
  const source = demo ? " This is synthetic playback using a fixture clock, not a live execution decision." : ""
  const stale = transportStale ? " The dashboard connection is stale; a newer decision may exist." : ""
  if (verdict === "REJECT" || verdict === "ESCALATE") {
    const outcome = verdict === "REJECT" ? "The recorded Guard decision rejected this proposal." : "The recorded Guard decision requires review or missing information before proceeding."
    return {
      label: `${verdict} · ${demo ? "DEMO" : "RECORDED"}`,
      detail: `${outcome}${reasons}${source}${stale}`,
      tone: verdict === "REJECT" ? "danger" : "warning",
    }
  }
  if (verdict !== "ALLOW") return { label: "UNKNOWN DECISION", detail: `The recorded verdict is missing or unsupported.${source}${stale}`, tone: "warning" }
  if (demo) return { label: "ALLOW · DEMO", detail: `Synthetic playback uses a fixture clock. This recorded ALLOW does not authorize live execution.${reasons}${stale}`, tone: "neutral" }
  if (transportStale) return { label: "ALLOW · STALE VIEW", detail: `The dashboard connection is stale. This stored ALLOW must not be treated as current execution permission.${reasons}`, tone: "warning" }

  const issued = timestamp(decision.checked_at)
  if (issued === null || !Number.isFinite(now) || issued > now) return {
    label: "ALLOW · INVALID TIME",
    detail: `The decision has a missing, malformed or future checked_at timestamp. Its validity cannot be established from this view.${reasons}`,
    tone: "warning",
  }
  if (!decision.valid_until) return {
    label: "ALLOW · NO EXPIRY",
    detail: `This legacy or incomplete record has no decision expiry. It cannot establish current execution permission.${reasons}`,
    tone: "warning",
  }
  const expires = timestamp(decision.valid_until)
  if (expires === null || expires <= issued) return {
    label: "ALLOW · INVALID WINDOW",
    detail: `The decision expiry is malformed or does not follow checked_at. This record cannot establish current execution permission.${reasons}`,
    tone: "warning",
  }
  if (now >= expires) return {
    label: "ALLOW · EXPIRED",
    detail: `This ALLOW expired at ${decision.valid_until}. A new Guard evaluation is required before execution.${reasons}`,
    tone: "warning",
  }
  return {
    label: "ALLOW · RECORDED",
    detail: `The recorded ALLOW has not reached its expiry (${decision.valid_until}) on the browser clock. The Executor must still obtain fresh state, recheck the decision and perform a conditional write. This dashboard badge is not execution permission.${reasons}`,
    tone: "good",
  }
}
