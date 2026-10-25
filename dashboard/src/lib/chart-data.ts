import { isNumber, percent } from "./data"
import type { Cluster, Target, Telemetry } from "./types"

export type ChartRange = "5m" | "15m" | "1h" | "all"
const windows: Record<Exclude<ChartRange, "all">, number> = { "5m": 300_000, "15m": 900_000, "1h": 3_600_000 }

export function timestamp(value?: string | null): number | null {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function fullTimestamp(value: unknown): string {
  if (!isNumber(value)) return "Unknown timestamp"
  const date = new Date(value)
  return `${date.toISOString()} · ${date.toLocaleString([], { timeZoneName: "short" })}`
}

export function timeTick(value: unknown): string {
  return isNumber(value) ? new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "Unknown"
}

/** Second-precision labels repeat across a short span, so show tenths of a second there. */
export function timeTicks(domain: [number, number] | null): (value: unknown) => string {
  if (!domain || domain[1] - domain[0] >= 10_000) return timeTick
  return value => isNumber(value) ? new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 1 }) : "Unknown"
}

export interface ResourcePoint { timestamp: number; cpu: number | null; memory: number | null }
export interface FaultMarker { timestamp: number; events: Telemetry[] }
export interface ResourceSeries {
  data: ResourcePoint[]
  markers: FaultMarker[]
  unscopedMarkers: number
  outsideMarkers: number
  domain: [number, number] | null
  retainedDomain: [number, number] | null
  requestedStart: number | null
  anchor: number
  limitedCoverage: boolean
}

/** The x coordinate is always the producer timestamp, never the position in a poll. */
export function resourceSeries(
  clusters: Cluster[], telemetry: Telemetry[] = [], range: ChartRange = "all", demo = false, now = Date.now(),
): ResourceSeries {
  const valid = clusters.flatMap(cluster => {
    const at = timestamp(cluster.observed_at)
    return at === null ? [] : [{ cluster, at }]
  }).sort((a, b) => a.at - b.at)
  const latest = valid.at(-1)?.cluster
  const scoped = valid.filter(({ cluster }) => cluster.cluster_id === latest?.cluster_id && cluster.namespace === latest?.namespace)
  const anchor = demo ? scoped.at(-1)?.at ?? now : now
  const retained = scoped.filter(({ at }) => at <= anchor)
  const requestedStart = range === "all" ? null : anchor - windows[range]
  // Repeated snapshots at the same producer timestamp are not extra samples.
  const points = new Map<number, ResourcePoint>()
  for (const { cluster, at } of retained) {
    if (requestedStart !== null && at < requestedStart) continue
    points.set(at, { timestamp: at, cpu: percent(cluster.summary.cpu_usage_cores, cluster.summary.cpu_allocatable_cores), memory: percent(cluster.summary.memory_usage_bytes, cluster.summary.memory_allocatable_bytes) })
  }
  const data = [...points.values()]
  const domain: [number, number] | null = data.length ? [data[0].timestamp, data[data.length - 1].timestamp] : null
  const retainedDomain: [number, number] | null = retained.length ? [retained[0].at, retained[retained.length - 1].at] : null
  const groups = new Map<number, Telemetry[]>()
  let unscopedMarkers = 0, outsideMarkers = 0
  for (const event of telemetry) {
    if (event.event_type !== "fault") continue
    if (!event.target) { unscopedMarkers++; continue }
    if (!latest || event.target.cluster_id !== latest.cluster_id || event.target.namespace !== latest.namespace) continue
    const at = timestamp(event.at)
    if (at === null || !domain || at < domain[0] || at > domain[1]) { outsideMarkers++; continue }
    groups.set(at, [...groups.get(at) ?? [], event])
  }
  return {
    data, domain, retainedDomain, requestedStart, anchor, unscopedMarkers, outsideMarkers,
    markers: [...groups.entries()].sort(([a], [b]) => a - b).map(([at, events]) => ({ timestamp: at, events })),
    limitedCoverage: requestedStart !== null && retainedDomain !== null && retainedDomain[0] > requestedStart,
  }
}

export interface ReadinessPoint { time: string; ready: number; desired: number; capturedAt?: string }
export function readinessSeries(points: ReadinessPoint[], range: ChartRange = "all", demo = false, now = Date.now()) {
  const timed = points.flatMap(point => {
    const at = timestamp(point.capturedAt)
    return at === null ? [] : [{ ...point, timestamp: at }]
  }).sort((a, b) => a.timestamp - b.timestamp)
  // Time-of-day labels alone cannot establish a date or a duration. Keep the
  // legacy display, but explicitly report that range filtering is unavailable.
  if (!timed.length) return { data: points, timed: false, anchor: null, limitedCoverage: false }
  const anchor = demo ? timed[timed.length - 1].timestamp : now
  const start = range === "all" ? null : anchor - windows[range]
  const data = timed.filter(point => point.timestamp <= anchor && (start === null || point.timestamp >= start))
  return { data, timed: true, anchor, limitedCoverage: start !== null && timed[0].timestamp > start }
}

export function readinessLabel(ready: boolean | null, observedAt?: string, transportStale = false, demo = false, now = Date.now()): string {
  const at = timestamp(observedAt)
  if (at === null || ready === null) return "Readiness unknown"
  const state = ready ? "Ready" : "NotReady"
  if (demo) return `SIMULATED ${state}`
  return transportStale || now - at > 15_000 || at > now ? `Last known ${state}` : state
}

/** Fault markers for one workload's readiness chart, limited to the plotted producer-time coverage. */
export function workloadFaultMarkers(telemetry: Telemetry[], target: Target | null | undefined, domain: [number, number] | null): FaultMarker[] {
  if (!target || !domain) return []
  const groups = new Map<number, Telemetry[]>()
  for (const event of telemetry) {
    if (event.event_type !== "fault" || !event.target) continue
    const scoped = event.target.cluster_id === target.cluster_id && event.target.namespace === target.namespace &&
      event.target.kind === target.kind && event.target.name === target.name
    const at = timestamp(event.at)
    if (!scoped || at === null || at < domain[0] || at > domain[1]) continue
    groups.set(at, [...groups.get(at) ?? [], event])
  }
  return [...groups.entries()].sort(([a], [b]) => a - b).map(([at, events]) => ({ timestamp: at, events }))
}
