import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ThemeToggle } from "./theme-toggle"

const mount = () => render(<TooltipProvider><ThemeToggle/></TooltipProvider>)

beforeEach(() => {
  localStorage.clear()
  document.documentElement.classList.add("dark")
})
afterEach(() => {
  localStorage.clear()
  document.documentElement.classList.add("dark")
  document.documentElement.style.colorScheme = "dark"
})

describe("Nightwatch theme preference", () => {
  it("switches both ways and restores the choice on remount", async () => {
    const view = mount()
    await userEvent.click(screen.getByRole("button", { name: "Switch to light mode" }))
    expect(document.documentElement).not.toHaveClass("dark")
    expect(document.documentElement.style.colorScheme).toBe("light")
    expect(localStorage.getItem("sentinel-theme")).toBe("light")
    view.unmount()
    mount()
    await userEvent.click(screen.getByRole("button", { name: "Switch to dark mode" }))
    expect(document.documentElement).toHaveClass("dark")
    expect(localStorage.getItem("sentinel-theme")).toBe("dark")
  })

  it("remains usable when browser storage is blocked", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError") })
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError") })
    mount()
    await userEvent.click(screen.getByRole("button", { name: "Switch to light mode" }))
    expect(document.documentElement).not.toHaveClass("dark")
    expect(screen.getByRole("button", { name: "Switch to dark mode" })).toBeVisible()
  })
})
