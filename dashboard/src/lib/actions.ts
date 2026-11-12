import type { RecoveryAction } from "./types"

// Plain descriptions of the typed actions in shared/contracts.py. Display only:
// the Guard and the Executor decide what an action may do.
const EFFECTS: Record<RecoveryAction, string> = {
  ROLLBACK: "Writes the reviewed baseline Pod template back to the Deployment.",
  RESTART: "Writes the unchanged reviewed template plus a restart token, so Kubernetes replaces every Pod. No configuration change.",
  SCALE: "Not implemented; the Guard and the Executor refuse it.",
  NO_ACTION: "No repair.",
  ESCALATE: "No repair; an operator reviews the incident.",
}

export function isRecoveryAction(value: unknown): value is RecoveryAction {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(EFFECTS, value)
}

/** What a recorded action does, or a fixed note for a value outside the contract. */
export function actionEffect(action: unknown): string {
  return isRecoveryAction(action) ? EFFECTS[action] : "Unknown action; not part of the wire contract."
}
