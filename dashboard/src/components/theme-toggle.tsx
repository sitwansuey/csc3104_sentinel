import { useLayoutEffect, useState } from "react"
import { Moon, Sun } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

type Theme = "dark" | "light"
const storageKey = "sentinel-theme"

function savedTheme(): Theme {
  try { return localStorage.getItem(storageKey) === "light" ? "light" : "dark" }
  catch { return "dark" }
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(savedTheme)
  useLayoutEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark")
    document.documentElement.style.colorScheme = theme
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#0a0c10" : "#f8f7f4")
    try { localStorage.setItem(storageKey, theme) } catch { /* Still works when browser storage is unavailable. */ }
  }, [theme])

  const label = theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
  return <Tooltip><TooltipTrigger asChild><Button variant="outline" size="sm" aria-label={label} onClick={() => setTheme(theme === "dark" ? "light" : "dark")} className="border-border bg-transparent dark:bg-transparent">
    {theme === "dark" ? <Sun className="size-3.5"/> : <Moon className="size-3.5"/>}<span className="hidden md:inline">{theme === "dark" ? "Light" : "Dark"}</span>
  </Button></TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>
}
