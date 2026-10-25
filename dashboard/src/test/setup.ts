import "@testing-library/jest-dom/vitest"
import { afterEach, vi } from "vitest"
import { cleanup } from "@testing-library/react"

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver=ResizeObserverStub
Object.defineProperty(HTMLElement.prototype,"getBoundingClientRect",{configurable:true,value:()=>new DOMRect(0,0,800,300)})
Object.defineProperty(window,"matchMedia",{writable:true,value:vi.fn().mockImplementation(()=>({matches:false,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn()}))})
Element.prototype.scrollIntoView=vi.fn()
Element.prototype.hasPointerCapture=()=>false
Element.prototype.setPointerCapture=vi.fn()
Element.prototype.releasePointerCapture=vi.fn()
if(!window.PointerEvent)window.PointerEvent=MouseEvent as typeof PointerEvent
afterEach(()=>{cleanup();vi.useRealTimers();vi.restoreAllMocks();location.hash=""})
