"""Read-only, bounded CSV telemetry tools. No generated code execution.

Text read from telemetry is quoted through agent/untrusted.py before it leaves
a tool: log examples carry a masked template and a short cleaned excerpt, or
only rule ids when a line looks like instructions aimed at the model.
"""
from pathlib import Path
import re
from datetime import UTC, timedelta, timezone
import pandas as pd

from .untrusted import label, quote, suspicious

PROFILE_SAMPLE_LIMIT = 80

TZ = timezone(timedelta(hours=8))


def error_mask(messages):
    text = messages.astype(str)
    errors = text.str.contains(r"error|fail|timeout|exception|oom|refused|unavailable", case=False, regex=True)
    # An allocation-triggered young GC is routine JVM telemetry, not a failure by itself.
    routine_gc = text.str.contains(r"GC \(Allocation Failure\)", case=False, regex=True)
    serious_gc = text.str.contains(r"Full GC|OutOfMemory|overhead limit|concurrent mode failure", case=False, regex=True)
    return errors & (~routine_gc | serious_gc)


def evidence_time(value, utc=False):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return None
    if utc:
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(TZ)
        return stamp.tz_convert(UTC).isoformat()
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def episodes(window, scores, *, utc=False):
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
        found.append({"onset": evidence_time(group["time"].iloc[0], utc),
            "end": evidence_time(group["time"].iloc[-1], utc),
            "samples": len(group), "duration_seconds": span, "peak_score": min(float(group["score"].max()), 1e12),
            "sustained": len(group) >= 3 and span >= 2 * cadence,
            "left_censored": group["time"].iloc[0] == table["time"].iloc[0]})
    return sorted(found, key=lambda e: (-e["sustained"], -e["duration_seconds"], -e["peak_score"]))[:5]


TIME_OF_DAY = r"\s*\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*"


def time_of_day_mask(series):
    """Values such as '15:33' carry no date; pandas would silently attach today's."""
    return series.astype(str).str.fullmatch(TIME_OF_DAY)


def timestamps(series):
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().all():
        magnitude = numeric.abs().median()
        unit = "ns" if magnitude > 1e17 else "us" if magnitude > 1e14 else "ms" if magnitude > 1e11 else "s"
        return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce").dt.tz_convert(TZ)
    # A time of day without a date is unparseable, never anchored to the current date.
    dated = series.where(~time_of_day_mask(series))
    return pd.to_datetime(dated, errors="coerce", format="mixed").apply(
        lambda t: t.tz_localize(TZ) if pd.notna(t) and t.tzinfo is None else t)


def bound(value):
    if not value:
        return None
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(TZ) if stamp.tzinfo is None else stamp


class Telemetry:
    def __init__(self, root, max_rows=1000000, *, file_tools=None, utc_evidence=False):
        self.root = Path(root).resolve()
        if not self.root.is_dir() or self.root.name != "telemetry":
            raise ValueError("Provide an existing telemetry directory, not the dataset parent")
        self.max_rows = max_rows
        self.utc_evidence = utc_evidence
        # Supplied by the data adapter, never by the model or CSV contents.
        self.file_tools = None if file_tools is None else {
            self.path(name).relative_to(self.root).as_posix(): tool for name, tool in file_tools.items()}
        if self.file_tools is not None and any(t not in {"metrics", "logs", "traces"} for t in self.file_tools.values()):
            raise ValueError("Invalid telemetry modality")

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
            if self.file_tools is not None:
                modality = self.file_tools.get(files[-1]["file"])
                files[-1]["allowed_tools"] = ["profile", modality] if modality else ["profile"]
        if not files:
            raise ValueError("No telemetry CSV files found")
        return files

    def run(self, tool, args):
        if tool not in {"profile", "metrics", "logs", "traces"}:
            raise ValueError(f"Unknown tool: {tool}")
        path = self.path(args["file"])
        if self.file_tools is not None and tool != "profile":
            modality = self.file_tools.get(path.relative_to(self.root).as_posix())
            if tool != modality:
                raise ValueError(f"Tool/data mismatch: {tool} cannot read this {modality or 'unregistered'} file; "
                                 "use the inventory allowed_tools. Missing modalities are unavailable.")
        if tool == "profile":
            frame = pd.read_csv(path, nrows=min(self.max_rows, 1000) + 1)
            truncated = len(frame) > min(self.max_rows, 1000)
            frame = frame.iloc[:min(self.max_rows, 1000)]
            if self.utc_evidence:
                for column in ("timestamp", "time", "startTime"):
                    if column in frame:
                        frame[column] = timestamps(frame[column]).map(lambda t: evidence_time(t, True))
            info = {"file": path.relative_to(self.root).as_posix(), "rows_scanned": len(frame), "truncated": False,
                    "profile_sample_only": truncated}
            # Sample text values are telemetry too: quoted, never copied raw.
            sample = [{column: quote(value, PROFILE_SAMPLE_LIMIT) if isinstance(value, str) else value
                       for column, value in row.items()} for row in frame.head(3).fillna("").to_dict("records")]
            info.update(columns=list(frame.columns), sample=sample,
                        distinct={c: int(frame[c].nunique()) for c in frame.columns})
            return info
        # Stream the full file; only incident-window rows are retained, under a hard cap.
        # Baselines use only rows before baseline_end (default: start), never the window under test.
        parts = []
        scanned = matched = invalid = kept = 0
        first_seen = last_seen = None
        time_col = args.get("time_column", "timestamp")
        component = args.get("component_column", "cmdb_id")
        value = args.get("value_column", "value")
        kpi = args.get("kpi_column", "kpi_name")
        prior_baseline = None
        log_baseline = None
        log_baseline_available = False
        start, end = bound(args.get("start")), bound(args.get("end"))
        baseline_end = bound(args.get("baseline_end")) or start
        for chunk in pd.read_csv(path, chunksize=100000):
            if time_col not in chunk or component not in chunk:
                raise ValueError("Missing time/component column; inspect inventory")
            if scanned == 0 and len(chunk) and bool(time_of_day_mask(chunk[time_col].dropna()).all()):
                raise ValueError(f"Time column '{time_col}' holds times of day without dates; "
                                 "choose a column with full dates or epoch values")
            scanned += len(chunk)
            chunk["_time"] = timestamps(chunk[time_col])
            invalid += int(chunk["_time"].isna().sum())
            if chunk["_time"].notna().any():
                low, high = chunk["_time"].min(), chunk["_time"].max()
                first_seen = low if first_seen is None else min(first_seen, low)
                last_seen = high if last_seen is None else max(last_seen, high)
            if tool == "logs" and baseline_end is not None:
                message = args.get("message_column", "message")
                if message not in chunk:
                    raise ValueError(f"Missing message column {message}")
                previous = chunk[(chunk["_time"] < baseline_end) & (chunk["_time"] >= baseline_end - pd.Timedelta(minutes=60))]
                if "timestamp_known" in previous:
                    previous = previous[previous["timestamp_known"].astype(str).str.lower() == "true"]
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
                if baseline_end is not None:
                    before = chunk[(chunk["_time"] < baseline_end) & (chunk["_time"] >= baseline_end - pd.Timedelta(minutes=60))]
                    prior = before.groupby([component, kpi]).agg(count=(value, "count"), total=(value, "sum"), square=("_square", "sum"))
                    prior_baseline = prior if prior_baseline is None else prior_baseline.add(prior, fill_value=0)
            mask = chunk["_time"].notna()
            if start is not None:
                mask &= chunk["_time"].ge(start)
            if end is not None:
                mask &= chunk["_time"].le(end)
            window = chunk.loc[mask]
            matched += len(window)
            if kept < self.max_rows:
                part = window.iloc[:self.max_rows-kept].copy()
                parts.append(part)
                kept += len(part)
        selected = pd.concat(parts, ignore_index=True) if parts else chunk.iloc[:0]
        info = {"file": path.relative_to(self.root).as_posix(), "rows_scanned": scanned, "rows_in_window": matched,
                "rows_retained": kept, "truncated": matched > kept, "invalid_timestamps": invalid}
        if matched == 0:
            # An empty window is absent evidence, not a healthy system.
            info["window_empty"] = True
            info["data_range"] = {"first": evidence_time(first_seen, self.utc_evidence) if first_seen is not None else None,
                                  "last": evidence_time(last_seen, self.utc_evidence) if last_seen is not None else None}
            info["note"] = "No rows fall inside the requested window; this file provides no evidence for it."
        if tool == "metrics":
            findings = []
            without_baseline = 0
            source = "pre_anomaly" if args.get("baseline_end") else "preceding_60_minutes"
            for keys, window in selected.groupby([component, kpi]):
                if prior_baseline is None or keys not in prior_baseline.index or prior_baseline.loc[keys, "count"] < 5:
                    without_baseline += 1
                    continue
                moments = prior_baseline.loc[keys]
                baseline_source = source
                center = float(moments["total"] / moments["count"])
                variance = max(0, float(moments["square"] / moments["count"] - center**2))
                scale = max(variance**.5, abs(center) * .01, 1e-9)
                scores = (window[value] - center).abs() / scale
                abnormal = window.loc[scores >= 3]
                if not abnormal.empty:
                    findings.append({"component": label(keys[0]), "kpi": label(keys[1]),
                        "baseline_source": baseline_source, "baseline_samples": int(moments["count"]),
                        "baseline_mean": center, "max_score": min(float(scores.max()), 1e12),
                        "anomaly_count": len(abnormal), "anomaly_fraction": len(abnormal)/len(window),
                        "first_occurrence": evidence_time(abnormal["_time"].min(), self.utc_evidence),
                        "peak_value": float(abnormal[value].max()), "episodes": episodes(window, scores, utc=self.utc_evidence)})
            info["findings"] = sorted(findings, key=lambda x: (
                -any(e["sustained"] and not e["left_censored"] for e in x["episodes"]), -x["max_score"]))[:30]
            info["total_findings"] = len(findings)
            # Series lacking >=5 healthy samples are not scored: a baseline containing the fault hides it.
            info["series_without_healthy_baseline"] = without_baseline
        elif tool == "logs":
            message = args.get("message_column", "message")
            if message not in selected:
                raise ValueError(f"Missing message column {message}")
            matches = selected[error_mask(selected[message])]
            info["error_count"] = len(matches)
            info["components"] = {label(k): int(v) for k, v in matches[component].value_counts().head(30).items()}
            info["examples"] = [{"component": label(r[component]), "time": evidence_time(r["_time"], True) if self.utc_evidence else str(r["_time"]), **quote(r[message])}
                                for _, r in matches.head(15).iterrows()]
            # Instruction-like lines anywhere in the window, not only error lines.
            texts = selected[message].astype(str)
            flagged = {text for text in texts.unique() if suspicious(text)}
            info["suspected_injection_lines"] = int(texts.isin(flagged).sum())
            # Minute-level error bursts retain onset timing instead of only early log samples.
            timed = matches
            if "timestamp_known" in timed:
                timed = timed[timed["timestamp_known"].astype(str).str.lower() == "true"]
            info["untimed_errors"] = len(matches) - len(timed)
            counts = timed.groupby([component, timed["_time"].dt.floor("min")]).size()
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
                        info["events"].append({"component": label(key[0]), "time": evidence_time(key[1], self.utc_evidence),
                            "error_count": int(count), "baseline_errors_per_minute": mean,
                            "left_censored": start is not None and key[1] <= start.floor("min")})
            info["events"] = sorted(info["events"], key=lambda e: -e["error_count"])[:30]
        else:
            duration = args.get("duration_column", "duration")
            if duration not in selected:
                raise ValueError(f"Missing duration column {duration}")
            selected = selected.copy()
            selected[duration] = pd.to_numeric(selected[duration], errors="coerce")
            info["latency"] = [{"component": label(key), "spans": len(group),
                                 "p95": float(group[duration].quantile(.95)),
                                 "max": float(group[duration].max())}
                                for key, group in selected.groupby(component) if group[duration].notna().any()]
            info["latency"] = sorted(info["latency"], key=lambda x: -x["p95"])["p95"][:30]
            info["events"] = []
            for key, group in selected.groupby(component):
                minute = group.groupby(group["_time"].dt.floor("min"))[duration].quantile(.95).dropna().sort_index()
                if len(minute) < 6:
                    continue
                center = float(minute.median())
                scale = max(float((minute-center).abs().median()) * 1.4826, abs(center)*.05, 1e-9)
                flags = minute > center + 3 * scale
                for time in minute.index[flags & ~flags.shift(fill_value=False)]:
                    info["events"].append({"component": label(key), "time": evidence_time(time, self.utc_evidence),
                        "p95": float(minute.loc[time]), "left_censored": time == minute.index[0]})
            info["events"] = sorted(info["events"], key=lambda e: -e["p95"])["p95"][:30]
            info["note"] = "Latency summaries alone do not establish parent-child causality."
        return info


CATALOG = """Tools (CSV files only):
profile(file): column names, first 3 rows, cardinalities. Inspect before choosing columns.
metrics(file,time_column,component_column,kpi_column,value_column,start,end): anomalies and persistent episodes against a healthy baseline (the 60 minutes before the window or known anomaly); ser[...]
logs(file,time_column,component_column,message_column,start,end): error counts and examples.
traces(file,time_column,component_column,duration_column,start,end): per-component span latency.
start/end are optional ISO date-times in UTC+8. Epoch s/ms/us/ns are detected.
All argument values are strings. Column names must match inventory. Full CSVs are streamed; incident-window rows are capped and truncation is flagged. A truncated window is incomplete evidence. A[...]
"""
