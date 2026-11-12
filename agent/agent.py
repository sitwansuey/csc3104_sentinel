import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import time
from .llm import PlanningBudgetExceeded
from .tools import CATALOG
from .workflows import retrieve
from .evidence import compact_evidence, corroborated_onsets, timestamp_rejection
from .progress import notify
from .untrusted import UNTRUSTED_KEY


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


STRING = {"type": "string"}
NODE = obj({"kind": {"enum": ["tool", "sequence", "selector", "parallel"]},
            "goal": STRING, "tool": {"enum": ["profile", "metrics", "logs", "traces"]},
            "args": {"type": "object", "additionalProperties": STRING},
            "children": {"type": "array", "items": {"$ref": "#/$defs/node"}}}, ["kind", "goal"])
PLAN = {**obj({"plan": {"$ref": "#/$defs/node"}}), "$defs": {"node": NODE}}
ANALYSIS = obj({"summary": STRING, "complete": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "next_steps": STRING})
FIELDS = ["root cause occurrence datetime", "root cause component", "root cause reason"]
FINAL = obj({"failures": {"type": "array", "maxItems": 10,
                         "items": obj({k: STRING for k in FIELDS}, [])},
             "explanation": STRING, "limitations": STRING})


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str, allow_nan=False), encoding="utf-8")


def bounded(data, limit=22000):
    """Prompt-only truncation is explicit; complete evidence remains on disk."""
    text = json.dumps(data, default=str)
    if len(text) <= limit:
        return data
    return {"context_truncated": True, "original_characters": len(text), "excerpt": text[:limit]}


def extract_candidates(context):
    reasons, components = [], []
    current = None
    for line in context.splitlines():
        trimmed = line.strip()
        if "POSSIBLE ROOT CAUSE REASONS" in trimmed:
            current = reasons
        elif "POSSIBLE ROOT CAUSE COMPONENTS" in trimmed:
            current = components
        elif trimmed.startswith("## ") and current is not None:
            current = None
        elif trimmed.startswith("- ") and current is not None:
            val = trimmed[2:].strip()
            if val and not val.startswith("(") and not val.endswith(")"):
                current.append(val)
    return sorted(set(reasons)), sorted(set(components))


def validate_report(final, required_fields, failure_count, allow_abstention, valid_reasons=None, valid_components=None):
    failures = final.get("failures")
    if not isinstance(failures, list) or len(failures) > 10:
        raise ValueError("Reporter returned invalid failures")
    if not failures and not allow_abstention:
        raise ValueError("Abstention is disabled: return the best-supported diagnosis")
    if failures and failure_count is not None and len(failures) != failure_count:
        raise ValueError("Reporter returned the wrong number of failures")
    prediction = {}
    for i, failure in enumerate(failures, 1):
        if not isinstance(failure, dict) or not failure or any(k not in FIELDS or not isinstance(v, str) or not v.strip() for k, v in failure.items()):
            raise ValueError("Reporter returned invalid root cause fields")
        if required_fields and set(failure) != set(required_fields):
            raise ValueError("Reporter did not match the public task output contract")
        if FIELDS[0] in failure:
            datetime.strptime(failure[FIELDS[0]], "%Y-%m-%d %H:%M:%S")
        if valid_reasons and FIELDS[2] in failure and failure[FIELDS[2]] not in valid_reasons:
            raise ValueError(f"Reporter root cause reason '{failure[FIELDS[2]]}' must be one of {valid_reasons}")
        if valid_components and FIELDS[1] in failure and failure[FIELDS[1]] not in valid_components:
            raise ValueError(f"Reporter root cause component '{failure[FIELDS[1]]}' must be one of {valid_components}")
        prediction[str(i)] = {k: failure[k] for k in FIELDS if k in failure}
    return prediction


def cite(final, evidence):
    """Check and strip each failure's evidence_ids; return (report without them, {number: ids}).

    Each failure must cite 1 to 10 ids of tool results this agent actually
    collected (evidence maps id to result). A missing or unknown id is invalid.
    """
    failures = final.get("failures") if isinstance(final, dict) else None
    if not isinstance(failures, list):
        raise ValueError("Reporter returned invalid failures")
    stripped, cited = [], {}
    for number, failure in enumerate(failures, 1):
        ids = failure.get("evidence_ids") if isinstance(failure, dict) else None
        if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(not isinstance(i, str) for i in ids):
            raise ValueError("Each failure must cite 1 to 10 evidence_ids of collected tool results")
        unknown = set(ids) - set(evidence)
        if unknown:
            raise ValueError(f"Failure {number} cites {len(unknown)} evidence id(s) that no tool result has")
        stripped.append({key: value for key, value in failure.items() if key != "evidence_ids"})
        cited[str(number)] = sorted(set(ids))
    return {**final, "failures": stripped}, cited


def named(result):
    """The component names a tool result's findings, events, examples, latency or components mention, casefolded."""
    names = set((result.get("components") or {}) if isinstance(result.get("components"), dict) else ())
    for key in ("findings", "events", "examples", "latency"):
        names.update(item.get("component") for item in result.get(key) or () if isinstance(item, dict))
    return {name.casefold() for name in names if isinstance(name, str)}


def mentions(result, component):
    """True when a tool result's findings, events, examples, latency or components name the component."""
    return component.casefold() in named(result)


def unsupported_citation(prediction, cited, evidence):
    """Why the cited results do not support the diagnosis, or None when they do.

    A cited result that names no component at all (for example a metrics scan
    with no findings, because the container never started) is neutral: it
    neither supports nor contradicts the diagnosis. A cited result that names
    only other components contradicts it, and at least one cited result must
    name the diagnosed component.
    """
    for number, failure in prediction.items():
        component = failure.get(FIELDS[1])
        if component is None:
            continue
        results = [evidence[i] for i in cited[number]]
        if any(named(result) and not mentions(result, component) for result in results):
            return f"A result cited for failure {number} does not name the diagnosed component."
        if not any(mentions(result, component) for result in results):
            return f"No result cited for failure {number} names the diagnosed component."
    return None


class Agent:
    def __init__(self, llm, telemetry, config, workspace, *, progress=None, deadline=None, clock=time.monotonic):
        """deadline: optional clock() value; no new round or report starts after it."""
        self.llm, self.telemetry, self.config = llm, telemetry, config
        self.deadline, self.clock = deadline, clock
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=False)
        self.steps = 0
        self.branch = 0
        self.evidence = []
        self.summaries = []
        self.cache = {}
        self.cache_hits = 0
        self.call_history = []
        # Evidence ids each reported failure cites, when run(cite_evidence=True).
        self.citations = {}
        self.round_ceiling = self.config.get("max_steps", 16)
        self.progress = progress

    def check_deadline(self, stage):
        if self.deadline is not None and self.clock() >= self.deadline:
            notify(self.progress, {"stage": stage, "status": "failed", "reason": "planning budget used up"})
            raise PlanningBudgetExceeded("Planning budget was used up before the " + stage)

    def call_key(self, tool, args):
        defaults = {"time_column": "timestamp", "component_column": "cmdb_id"}
        defaults.update({"metrics": {"value_column": "value", "kpi_column": "kpi_name"},
                         "logs": {"message_column": "message"},
                         "traces": {"duration_column": "duration"}}.get(tool, {}))
        normalized = {**(defaults if tool != "profile" else {}), **args}
        if "file" in normalized:
            normalized["file"] = self.telemetry.path(normalized["file"]).relative_to(self.telemetry.root).as_posix()
        for name in ("start", "end"):
            if normalized.get(name):
                stamp = datetime.fromisoformat(normalized[name])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone(timedelta(hours=8)))
                normalized[name] = stamp.astimezone(timezone.utc).isoformat()
        return json.dumps([tool, normalized], sort_keys=True)

    def ensure_coverage(self, inventory):
        """Reserve deterministic checks for available but unexamined modalities."""
        scopes = {}
        for call in self.call_history:
            args = call["args"]
            if args.get("start") and args.get("end") and args.get("file"):
                date = Path(args["file"]).parts[0]
                scopes[(date, args["start"], args["end"])] = True
        for date, start, end in scopes:
            files = [f for f in inventory if f["file"].startswith(date + "/")]
            for modality, tool in [("log", "logs"), ("trace", "traces"), ("metric", "metrics")]:
                for file in files:
                    if Path(file["file"]).parent.name != modality:
                        continue
                    cols = file["columns"]
                    if tool == "metrics" and not ("value" in cols and ("kpi_name" in cols or "name" in cols)):
                        continue
                    def choose(*names):
                        return next((n for n in names if n in cols), None)
                    args = {"file": file["file"], "start": start, "end": end,
                            "time_column": choose("timestamp", "startTime"),
                            "component_column": choose("cmdb_id", "serviceName", "tc")}
                    if tool == "logs":
                        args["message_column"] = choose("message", "value", "content")
                    elif tool == "traces":
                        args["duration_column"] = choose("duration", "elapsedTime")
                    else:
                        args.update(kpi_column=choose("kpi_name", "name"), value_column="value")
                    if any(v is None for v in args.values()):
                        continue
                    if self.steps >= self.round_ceiling:
                        return
                    if self.call_key(tool, args) not in self.cache:
                        self.execute({"kind": "tool", "goal": "Coverage check for unexamined telemetry", "tool": tool, "args": args})

    def execute(self, node, parent="root", depth=0):
        if not isinstance(node, dict):
            return False, [{"error": "Plan node must be an object"}]
        if depth > self.config.get("max_depth", 4) or self.branch >= self.config.get("max_steps", 16) * 3:
            return False, [{"error": "depth budget exceeded"}]
        self.branch += 1
        branch = f"branch-{self.branch:03d}"
        directory = self.workspace / branch
        directory.mkdir()
        save(directory / "task.json", {"parent": parent, "node": node})
        kind = node.get("kind")
        outputs = []
        success = False
        if kind == "tool":
            tool, raw_args = node.get("tool"), node.get("args", {})
            args = dict(raw_args) if isinstance(raw_args, dict) else {}
            if "file" in args:
                try:
                    args["file"] = self.telemetry.path(args["file"]).relative_to(self.telemetry.root).as_posix()
                except (ValueError, OSError):
                    pass
            try:
                key = self.call_key(tool, args)
            except (ValueError, KeyError, TypeError):
                key = json.dumps([tool, args], sort_keys=True)
            if key in self.cache:
                self.cache_hits += 1
                success, cached = self.cache[key]
                outputs = [{**item, "cache_hit": True, "reused_by": branch} for item in cached]
                notify(self.progress, {"stage": "tool", "status": "completed", "tool": tool,
                                       "cache_hit": True, "evidence_id": branch})
            elif self.steps >= min(self.config.get("max_steps", 16), self.round_ceiling):
                outputs = [{"error": "tool budget exhausted"}]
            else:
                self.steps += 1
                notify(self.progress, {"stage": "tool", "status": "started", "tool": tool,
                                       "evidence_id": branch})
                try:
                    result = self.telemetry.run(node["tool"], args)
                    # Each quoted log line gets its own evidence id inside this branch.
                    for number, example in enumerate(result.get("examples", []), 1):
                        example["evidence_id"] = f"{branch}.{number}"
                    outputs = [{"evidence_id": branch, "tool": node["tool"], "result": result}]
                    success = not result.get("truncated", False)
                    if node["tool"] == "metrics":
                        success &= bool(result.get("findings"))
                    elif node["tool"] == "logs":
                        success &= result.get("error_count", 0) > 0
                    elif node["tool"] == "traces":
                        success &= bool(result.get("latency"))
                except (ValueError, KeyError, OSError) as exc:
                    outputs = [{"evidence_id": branch, "error": str(exc)}]
                self.cache[key] = (success, outputs)
                self.call_history.append({"tool": tool, "args": args, "evidence_id": branch,
                                          "success": bool(success)})
                self.evidence.extend(outputs)
                notify(self.progress, {"stage": "tool", "status": "completed" if "result" in outputs[0] else "failed",
                                       "tool": tool, "evidence_id": branch, "tool_calls": self.steps})
        elif kind in {"sequence", "selector", "parallel"}:
            children = node.get("children", [])
            statuses = []
            for child in children[:self.config.get("max_steps", 16)]:
                ok, results = self.execute(child, branch, depth + 1)
                statuses.append(ok)
                outputs.extend(results)
                if (kind == "sequence" and not ok) or (kind == "selector" and ok):
                    break
            # Parallel means independent/all-required branches; serialized for local memory use.
            success = bool(statuses) and (any(statuses) if kind == "selector" else all(statuses))
        else:
            outputs = [{"error": f"Invalid node kind: {kind}"}]
        save(directory / "result.json", {"state": "success" if success else "failure", "outputs": outputs})
        return success, outputs

    def run(self, instruction, context="", required_fields=None, failure_count=None, initial_plan=None,
            cite_evidence=False):
        """Investigate and report; return the validated prediction ({} for an abstention).

        cite_evidence (the live planning path): each reported failure must also
        cite the evidence ids of collected tool results, at least one of which
        names its component (see unsupported_citation). A report still invalid
        after its one correction, or one whose citations do not support the
        component, becomes an abstention.
        """
        allow_abstention = self.config.get("allow_abstention", False)
        inventory = self.telemetry.inventory()
        # Exact inventory is persisted; a bounded subset is exposed and explicitly labelled.
        save(self.workspace / "inventory.json", inventory)
        save(self.workspace / "config.json", self.config)
        base = {"instruction": instruction, "system_context": context,
                "inventory": inventory[:150], "inventory_omitted": max(0, len(inventory)-150),
                "workflows": retrieve(instruction)}
        for round_id in range(self.config.get("max_rounds", 3)):
            self.check_deadline("investigation")
            notify(self.progress, {"stage": "investigation", "status": "started", "round": round_id + 1})
            before_steps = self.steps
            self.round_ceiling = min(self.config.get("max_steps", 16), self.steps + 6)
            prompt = json.dumps({**base, "prior_analysis": bounded(self.summaries, 6000),
                                "completed_calls": self.call_history,
                                UNTRUSTED_KEY: bounded(compact_evidence(self.evidence), 10000),
                                "remaining_tools": self.config.get("max_steps", 16)-self.steps})
            plan = initial_plan if round_id == 0 and initial_plan is not None else self.llm.ask("Planner", prompt + "\n" + CATALOG +
                "\nProduce a bounded behavior tree. NEVER repeat a completed call; cached results provide no new evidence. Follow up on unexamined modalities, changed windows or columns. Distinguish sustained episodes from isolated spikes and boundary-censored runs. Do not select the earliest anomaly as root cause. Use sequence for dependencies, selector for fallback, parallel for independent analyses. A metrics/logs leaf with no findings fails its diagnostic condition; truncated results also fail. Use parallel when all modalities should be inspected even if one is empty. Plan only relevant dates. No more than 6 tool leaves per round.", PLAN)
            save(self.workspace / f"plan-{round_id}.json", plan)
            _, outputs = self.execute(plan.get("plan", {}))
            self.round_ceiling = min(self.config.get("max_steps", 16), self.steps + 3)
            self.ensure_coverage(inventory)
            candidates = corroborated_onsets(self.evidence)
            analysis = self.llm.ask("Analyzer", json.dumps({"task": instruction,
                "system_context": context,
                UNTRUSTED_KEY: bounded(compact_evidence(self.evidence)), "corroborated_onsets": candidates,
                "previous": bounded(self.summaries, 6000)}) + "\nAssess evidence sufficiency and contradictions. Earliest anomaly does not imply root cause. Require persistent onset and independently corroborating signals, with temporal order consistent with propagation. Corroborated onsets are hypotheses, not proven causes. Name evidence IDs. State exact NEW tool/column/window corrections when needed. Do not equate successful tools with a solved diagnosis.", ANALYSIS)
            confidence = analysis.get("confidence", 0)
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise ValueError("Analyzer returned invalid confidence")
            self.summaries.append(analysis)
            save(self.workspace / f"analysis-{round_id}.json", analysis)
            save(self.workspace / "call-history.json", self.call_history)
            save(self.workspace / "corroborated-onsets.json", candidates)
            if self.steps == before_steps:
                save(self.workspace / "stop.json", {"reason": "No new tool calls; stopped stalled replanning"})
                break
            if (analysis.get("complete") is True and confidence >= self.config.get("confidence_threshold", .8)
                and (not required_fields or FIELDS[0] not in required_fields or candidates)) or self.steps >= self.config.get("max_steps", 16):
                break
        self.check_deadline("report")
        valid_reasons, valid_components = extract_candidates(context)
        final_schema = json.loads(json.dumps(FINAL))
        if required_fields:
            item_props = {}
            for k in required_fields:
                if k == FIELDS[2] and valid_reasons:
                    item_props[k] = {"type": "string", "enum": valid_reasons}
                elif k == FIELDS[1] and valid_components:
                    item_props[k] = {"type": "string", "enum": valid_components}
                else:
                    item_props[k] = STRING
            final_schema = obj({"failures": {"type": "array", "maxItems": failure_count or 10,
                "items": obj(item_props, required_fields)},
                "explanation": STRING, "limitations": STRING})
        else:
            if valid_reasons:
                final_schema["properties"]["failures"]["items"]["properties"][FIELDS[2]] = {
                    "type": "string", "enum": valid_reasons}
            if valid_components:
                final_schema["properties"]["failures"]["items"]["properties"][FIELDS[1]] = {
                    "type": "string", "enum": valid_components}
        # Ids of the tool results collected so far: the only evidence a report may cite.
        results = {item["evidence_id"]: item["result"] for item in self.evidence if "result" in item}
        if cite_evidence and results:
            items = final_schema["properties"]["failures"]["items"]
            items["properties"]["evidence_ids"] = {"type": "array", "minItems": 1, "maxItems": 10,
                                                   "items": {"type": "string", "enum": sorted(results)}}
            items["required"] = [*items["required"], "evidence_ids"]
        candidates = corroborated_onsets(self.evidence)
        if not allow_abstention:
            final_schema["properties"]["failures"]["minItems"] = failure_count or 1
            if not required_fields:
                final_schema["properties"]["failures"]["items"]["minProperties"] = 1
        if allow_abstention and required_fields and FIELDS[0] in required_fields and candidates:
            final_schema["properties"]["failures"]["items"]["properties"][FIELDS[0]] = {
                "type": "string", "enum": sorted({c["time"] for c in candidates})}
        report_context = json.dumps({"task": instruction, "system_context": context,
            "required_fields": required_fields, "number_of_failures": failure_count,
            "analysis": bounded(self.summaries, 6000), "corroborated_onsets": candidates,
            UNTRUSTED_KEY: bounded(compact_evidence(self.evidence))})
        report_instructions = (
            "\nReport failures chronologically. Use exact component/reason vocabulary from the task or system context. "
            "Datetimes must be YYYY-MM-DD HH:MM:SS in UTC+8. Choose a causally plausible initiating event, "
            "NEVER simply the earliest anomaly. Include only requested root cause fields. Return the requested "
            "number of failures, not a list of symptoms. Explain supporting evidence IDs, contradictions, "
            "and limitations outside failures. "
        )
        if valid_reasons:
            report_instructions += f" 'root cause reason' MUST be chosen exactly from: {json.dumps(valid_reasons)}."
        if valid_components:
            report_instructions += f" 'root cause component' MUST be chosen exactly from: {json.dumps(valid_components)}."
        if allow_abstention:
            report_instructions += "Select timestamps only from corroborated_onsets. If no candidate is supported, abstain with an empty failures list."
        else:
            report_instructions += (
                "Abstention is disabled. Always return your best-supported diagnosis, even when the analyzer "
                "is incomplete or uncertain. Do not return an empty failures list, blank fields, or unknown placeholders. "
                "Corroborated onsets are helpful candidates, not an exhaustive list: consider all observed evidence "
                "when estimating a timestamp. Low confidence does not block an answer. Clearly state uncertainty "
                "and missing evidence in explanation/limitations; do not present estimates as proven facts."
            )
        if cite_evidence:
            report_instructions += (
                " Each failure MUST list evidence_ids: the IDs of the collected tool results that show this "
                "component and reason. At least one cited result must name the component; do not cite results that name "
                "only other components.")

        def check(answer):
            stripped, cited = cite(answer, results) if cite_evidence else (answer, {})
            return validate_report(stripped, required_fields, failure_count, allow_abstention,
                                   valid_reasons, valid_components), cited

        refusal = None  # cite_evidence only: why the report is replaced by an abstention
        if cite_evidence and not results:
            final, prediction, cited = {"failures": []}, {}, {}
            refusal = "No tool result was collected, so no diagnosis can cite evidence."
        else:
            final = self.llm.ask("Reporter", report_context + report_instructions, final_schema)
            try:
                prediction, cited = check(final)
            except ValueError as exc:
                save(self.workspace / "invalid-report.json", final)
                final = self.llm.ask("Reporter", report_context + report_instructions +
                    "\nCorrect the invalid response: " + str(exc) + "\nPrevious response: " + json.dumps(final), final_schema)
                try:
                    prediction, cited = check(final)
                except ValueError as again:
                    if not cite_evidence:
                        raise
                    # One correction only: a report still outside the code-owned vocabulary abstains.
                    save(self.workspace / "invalid-correction.json", final)
                    final, prediction, cited = {"failures": []}, {}, {}
                    refusal = "The corrected report was still invalid: " + str(again)
            if refusal is None and cite_evidence and prediction:
                refusal = unsupported_citation(prediction, cited, results)
        if refusal is not None:
            if not allow_abstention:
                raise ValueError(refusal)
            if final.get("failures"):
                save(self.workspace / "rejected-report.json", final)
            final = {"failures": [], "explanation": "Abstained: " + refusal,
                     "limitations": "A diagnosis must use the code-owned vocabulary and cite collected "
                                    "evidence that names its component."}
            prediction, cited = {}, {}
        self.citations = cited
        failures = final["failures"]
        rejection = timestamp_rejection(failures, candidates, self.summaries[-1] if self.summaries else {},
                                        self.config.get("confidence_threshold", .8))
        if rejection and allow_abstention:
            save(self.workspace / "rejected-report.json", final)
            final = {"failures": [], "explanation": "Abstained: " + rejection,
                     "limitations": "A complete diagnosis and temporal corroboration are required; candidate guesses are rejected."}
            prediction = {}
        elif rejection:
            final["limitations"] = (final.get("limitations", "") + " Best-effort estimate: " + rejection).strip()
        save(self.workspace / "report.json", final)
        save(self.workspace / "prediction.json", prediction)
        save(self.workspace / "usage.json", self.llm.calls)
        save(self.workspace / "execution-summary.json", {"tool_calls": self.steps,
            "cache_hits": self.cache_hits, "corroborated_candidates": len(candidates),
            "allow_abstention": allow_abstention, "timestamp_evidence_warning": rejection,
            "abstained": not bool(prediction),
            **({"citations": self.citations, "citation_refusal": refusal} if cite_evidence else {}),
            "modalities": sorted({e["tool"] for e in self.evidence if "result" in e})})
        return prediction
