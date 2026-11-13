import { describe, expect, it } from "vitest"
import { actionEffect, isRecoveryAction } from "./actions"

describe("typed recovery action descriptions", () => {
  it("describes every contract action and says RESTART changes no configuration", () => {
    for (const action of ["ROLLBACK", "RESTART", "SCALE", "NO_ACTION", "ESCALATE"]) {
      expect(isRecoveryAction(action)).toBe(true)
      expect(actionEffect(action)).not.toContain("Unknown")
    }
    expect(actionEffect("RESTART")).toContain("No configuration change")
    expect(actionEffect("ROLLBACK")).toContain("reviewed baseline")
    expect(actionEffect("SCALE")).toContain("replica count recorded with the reviewed baseline")
    expect(actionEffect("SCALE")).toContain("no configuration change")
  })

  it("never trusts a value outside the contract", () => {
    for (const value of ["restart", "toString", "__proto__", "", undefined, 3, null]) {
      expect(isRecoveryAction(value)).toBe(false)
      expect(actionEffect(value)).toBe("Unknown action; not part of the wire contract.")
    }
  })
})
