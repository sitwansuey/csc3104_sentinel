"""Read-only, bounded CSV telemetry tools. No generated code execution."""
from pathlib import Path
import re
from datetime import timezone, timedelta
import pandas as pd

TZ = timezone(timedelta(hours=8))


def error_mask(messages):
    text = messages.astype(str)
    errors = text.str.contains(r"error|fail|timeout|exception|oom|refused|unavailable", case=False, regex=True)
    # An allocation-triggered young GC is routine JVM telemetry, not a failure by itself.
    routine_gc = text.str.contains(r"GC \(Allocation Failure\)", case=False, regex=True)
    serious_gc = text.str.contains(r"Full GC|OutOfMemory|overhead limit|concurrent mode failure", case=False, regex=True)
    return errors & (~routine_gc | serious_gc)


def episodes(window, scores):
    """Separate isolated spikes from runs; never treat a censored run as an onset."""
    table = pd.DataFrame({"time": window["_time"], "score": scores}).dropna().sort_values("time")
    table = table.groupby("time", as_index=False)["score"].max()
    if table.empty:
        return []
    gaps = table["time"].diff().dt.total_seconds()
    cadence = float(gaps[gaps > 0].median()) if (gaps > 0).any() else 60.
    flags = table["score"] >= 3
    groups = ((flags != flags.shift()) | (gaps > cadence * 2)).cumsum()
    found = []
    for _, group in table[flags].groupby(groups[flags]):
        span = (group["time"].iloc[-1] - group["time"].iloc[0]).total_seconds()
        found.append({"onset": group["time"].iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
            "end": group["time"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
            "samples": len(group), "duration_seconds": span, "peak_score": min(float(group["score"].max()), 1e12),
            "sustained": len(group) >= 3 and span >= 2 * cadence,
            "left_censored": group["time"].iloc[0] == table["time"].iloc[0]})
    return sorted(found, key=lambda e: (-e["sustained"], -e["duration_seconds"], -e["peak_score"]))[:5]


def timestamps(series):
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().all():
        magnitude = numeric.abs().median()
        unit = "ns" if magnitude > 1e17 else "us" if magnitude > 1e14 else "ms" if magnitude > 1e11 else "s"
        return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce").dt.tz_convert(TZ)
    return pd.to_datetime(series, errors="coerce", format="mixed").apply(
        lambda t: t.tz_localize(TZ) if pd.notna(t) and t.tzinfo is None else t)


class Telemetry:
    def __init__(self, root, max_rows=1000000):
        self.root = Path(root).resolve()
        if not self.root.is_dir() or self.root.name != "telemetry":
            raise ValueError("Provide an existing telemetry directory, not the dataset parent")
        self.max_rows = max_rows

    def path(self, relative):
        clean = str(relative).strip().replace("\\", "/")
        if "/telemetry/" in clean:
            clean = clean.split("/telemetry/")[-1]
        elif clean.startswith("telemetry/"):
            clean = clean[len("telemetry/"):]
        elif clean.startswith("/telemetry/"):
            clean = clean[len("/telemetry/"):]
        else:
            m = re.search(r"\b\d{4}[_-]\d{2}[_-]\d{2}/.*", clean)
            if m:
                clean = m.group(0)
        clean = clean.lstrip("/")
        path = (self.root / clean).resolve()
        if not path.is_relative_to(self.root) or path.suffix.lower() != ".csv" or path.name in {"query.csv", "record.csv"}:
            raise ValueError("Only CSV files inside telemetry are accessible")
        return path

    def inventory(self):
        files = []
        for p in sorted(self.root.rglob("*.csv")):
            self.path(str(p.relative_to(self.root)))
            files.append({"file": p.relative_to(self.root).as_posix(), "bytes": p.stat().st_size,
                          "columns": list(pd.read_csv(p, nrows=0).columns)})
        if not files:
            raise ValueError("No telemetry CSV files found")
        return files

    def run(self, tool, args):
        if tool not in {"profile", "metrics", "logs", "traces"}:
            raise ValueError(f"Unknown tool: {tool}")
        path = self.path(args["file"])
        if tool == "profile":
            frame = pd.read_csv(path, nrows=min(self.max_rows, 1000) + 1)
            truncated = len(frame) > min(self.max_rows, 1000)
            frame = frame.iloc[:min(self.max_rows, 1000)]
            info = {"file": path.relative_to(self.root).as_posix(), "rows_scanned": len(frame), "truncated": False,
                    "profile_sample_only": truncated}
            info.update(columns=list(frame.columns), sample=frame.head(3).fillna("").to_dict("records"),
                        distinct={c: int(frame[c].nunique()) for c in frame.columns})
            return info
        # Stream the full file; only incident-window rows are retained, under a hard cap.
        # Baseline moments aggregate every metric row before window filtering.
        parts = []
        scanned = matched = invalid = kept = 0
        time_col = args.get("time_column", "timestamp")
        component = args.get("component_column", "cmdb_id")
        value = args.get("value_column", "value")
        kpi = args.get("kpi_column", "kpi_name")
        baseline = None
        prior_baseline = None
        log_baseline = None
        log_baseline_available = False
        start = pd.Timestamp(args["start"]) if args.get("start") else None
        if start is not None and start.tzinfo is None:
            start = start.tz_localize(TZ)
        for chunk in pd.read_csv(path, chunksize=100000):
            if time_col not in chunk or component not in chunk:
                raise ValueError("Missing time/component column; inspect inventory")
            scanned += len(chunk)
            chunk["_time"] = timestamps(chunk[time_col])
            invalid += int(chunk["_time"].isna().sum())
            if tool == "logs" and start is not None:
                message = args.get("message_column", "message")
                if message not in chunk:
                    raise ValueError(f"Missing message column {message}")
                previous = chunk[(chunk["_time"] < start) & (chunk["_time"] >= start - pd.Timedelta(minutes=60))]
                log_baseline_available |= not previous.empty
                previous = previous[error_mask(previous[message])]
                counts = previous.groupby([component, previous["_time"].dt.floor("min")]).size()
                log_baseline = counts if log_baseline is None else log_baseline.add(counts, fill_value=0)
            if tool == "metrics":
                if value not in chunk:
                    raise ValueError(f"Missing metric value column: {value}")
                if kpi not in chunk:
                    # Wide-format metrics: analyze the explicitly selected numeric column.
                    kpi = "_kpi"
                    chunk[kpi] = value
                chunk[value] = pd.to_numeric(chunk[value], errors="coerce")
                chunk[value] = chunk[value].replace([float("inf"), -float("inf")], float("nan"))
                chunk["_square"] = chunk[value] ** 2
                moments = chunk.groupby([component, kpi]).agg(count=(value, "count"), total=(value, "sum"), square=("_square", "sum"))
                baseline = moments if baseline is None else baseline.add(moments, fill_value=0)
                if start is not None:
                    before = chunk[(chunk["_time"] < start) & (chunk["_time"] >= start - pd.Timedelta(minutes=60))]
                    prior = before.groupby([component, kpi]).agg(count=(value, "count"), total=(value, "sum"), square=("_square", "sum"))
                    prior_baseline = prior if prior_baseline is None else prior_baseline.add(prior, fill_value=0)
            mask = chunk["_time"].notna()
            for bound, op in [("start", "ge"), ("end", "le")]:
                if args.get(bound):
                    t = pd.Timestamp(args[bound])
                    t = t.tz_localize(TZ) if t.tzinfo is None else t
                    mask &= getattr(chunk["_time"], op)(t)
            window = chunk.loc[mask]
            matched += len(window)
            if kept < self.max_rows:
                part = window.iloc[:self.max_rows-kept].copy()
                parts.append(part)
                kept += len(part)
        selected = pd.concat(parts, ignore_index=True) if parts else chunk.iloc[:0]
        info = {"file": path.relative_to(self.root).as_posix(), "rows_scanned": scanned, "rows_in_window": matched,
                "rows_retained": kept, "truncated": matched > kept, "invalid_timestamps": invalid}
        if tool == "metrics":
            findings = []
            for keys, window in selected.groupby([component, kpi]):
                moments = baseline.loc[keys]
                baseline_source = "full_series_fallback"
                if prior_baseline is not None and keys in prior_baseline.index and prior_baseline.loc[keys, "count"] >= 5:
                    moments = prior_baseline.loc[keys]
                    baseline_source = "preceding_60_minutes"
                if moments["count"] == 0:
                    continue
                center = float(moments["total"] / moments["count"])
                variance = max(0, float(moments["square"] / moments["count"] - center**2))
                scale = max(variance**.5, abs(center) * .01, 1e-9)
                scores = (window[value] - center).abs() / scale
                abnormal = window.loc[scores >= 3]
                if not abnormal.empty:
                    findings.append({"component": str(keys[0]), "kpi": str(keys[1]),
                        "baseline_source": baseline_source, "baseline_samples": int(moments["count"]),
                        "baseline_mean": center, "max_score": min(float(scores.max()), 1e12),
                        "anomaly_count": len(abnormal), "anomaly_fraction": len(abnormal)/len(window),
                        "first_occurrence": abnormal["_time"].min().strftime("%Y-%m-%d %H:%M:%S"),
                        "peak_value": float(abnormal[value].max()), "episodes": episodes(window, scores)})
            info["findings"] = sorted(findings, key=lambda x: (
                -any(e["sustained"] and not e["left_censored"] for e in x["episodes"]), -x["max_score"]))[:30]
            info["total_findings"] = len(findings)
        elif tool == "logs":
            message = args.get("message_column", "message")
            if message not in selected:
                raise ValueError(f"Missing message column {message}")
            matches = selected[error_mask(selected[message])]
            info["error_count"] = len(matches)
            info["components"] = {str(k): int(v) for k, v in matches[component].value_counts().head(30).items()}
            info["examples"] = [{"component": str(r[component]), "time": str(r["_time"]),
                                 "message": str(r[message])[:500]} for _, r in matches.head(15).iterrows()]
            # Minute-level error bursts retain onset timing instead of only early log samples.
            counts = matches.groupby([component, matches["_time"].dt.floor("min")]).size()
            info["events"] = []
            info["burst_baseline_available"] = bool(log_baseline_available)
            if log_baseline_available:
                for key, count in counts.items():
                    component_counts = log_baseline.loc[key[0]] if key[0] in log_baseline.index.get_level_values(0) else pd.Series(dtype=float)
                    mean = float(component_counts.sum()) / 60
                    variance = max(0, float((component_counts**2).sum()) / 60 - mean**2)
                    threshold = max(3, mean + 3 * variance**.5)
                    prior_key = (key[0], key[1] - pd.Timedelta(minutes=1))
                    if count >= threshold and counts.get(prior_key, 0) < threshold:
                        info["events"].append({"component": str(key[0]), "time": key[1].strftime("%Y-%m-%d %H:%M:%S"),
                            "error_count": int(count), "baseline_errors_per_minute": mean,
                            "left_censored": start is not None and key[1] <= start.floor("min")})
            info["events"] = sorted(info["events"], key=lambda e: -e["error_count"])[:30]
        else:
            duration = args.get("duration_column", "duration")
            if duration not in selected:
                raise ValueError(f"Missing duration column {duration}")
            selected = selected.copy()
            selected[duration] = pd.to_numeric(selected[duration], errors="coerce")
            info["latency"] = [{"component": str(key), "spans": len(group),
                                 "p95": float(group[duration].quantile(.95)),
                                 "max": float(group[duration].max())}
                                for key, group in selected.groupby(component) if group[duration].notna().any()]
            info["latency"] = sorted(info["latency"], key=lambda x: -x["p95"])[:30]
            info["events"] = []
            for key, group in selected.groupby(component):
                minute = group.groupby(group["_time"].dt.floor("min"))[duration].quantile(.95).dropna().sort_index()
                if len(minute) < 6:
                    continue
                center = float(minute.median())
                scale = max(float((minute-center).abs().median()) * 1.4826, abs(center)*.05, 1e-9)
                flags = minute > center + 3 * scale
                for time in minute.index[flags & ~flags.shift(fill_value=False)]:
                    info["events"].append({"component": str(key), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "p95": float(minute.loc[time]), "left_censored": time == minute.index[0]})
            info["events"] = sorted(info["events"], key=lambda e: -e["p95"])[:30]
            info["note"] = "Latency summaries alone do not establish parent-child causality."
        return info


CATALOG = """Tools (CSV files only):
profile(file): column names, first 3 rows, cardinalities. Inspect before choosing columns.
metrics(file,time_column,component_column,kpi_column,value_column,start,end): anomalies and persistent episodes against the preceding 60 minutes (full-series fallback if insufficient baseline). Onset is not the earliest outlier; left_censored episodes have unknown onset. For wide data choose ONE value_column and omit kpi_column.
logs(file,time_column,component_column,message_column,start,end): error counts and examples.
traces(file,time_column,component_column,duration_column,start,end): per-component span latency.
start/end are optional ISO date-times in UTC+8. Epoch s/ms/us/ns are detected.
All argument values are strings. Column names must match inventory. Full CSVs are streamed; incident-window rows are capped and truncation is flagged. A truncated window is incomplete evidence. Always specify start/end from the task.
"""
