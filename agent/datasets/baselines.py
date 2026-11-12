"""Non-LLM RCAEval baselines, run through the pinned upstream RCAEval package.

- 'baro' calls RCAEval.e2e.baro.baro: BARO's robust root-cause scorer (Pham, Ha
  and Zhang, FSE 2024). Each metric is scaled by the median and interquartile
  range of the samples before the inject time, and metrics are ranked by their
  largest scaled value after it. RCAEval passes the known inject time, so BARO's
  change-point detection step is not used, as in the RCAEval benchmark.
- 'anomaly-rank' calls RCAEval.e2e.nsigma: the same ranking with the mean and
  standard deviation (N-Sigma). It is the plain anomaly-ranking baseline.

Neither method sees labels: both read only the blinded metrics.parquet of a
task, cut to its standard window. The upstream functions are called unchanged;
'dataset' is the task's lower-case dataset name (for example 're2-ob'), which
upstream preprocessing uses to drop the time and constant columns and convert
memory to MB.
"""

from .evaluate import metric_frame, to_service

# A ranking is cut once it holds this many distinct services and indicators. AC@1..5
# and Avg@5 are then unchanged at both service and indicator level; MRR beyond rank 5 is lost.
KEEP = 5


def upstream(method):
    """The pinned upstream ranking function for a baseline name."""
    if method == "baro":
        from RCAEval.e2e.baro import baro
        return baro
    if method == "anomaly-rank":
        from RCAEval.e2e import nsigma
        return nsigma
    raise ValueError("Unknown baseline: " + str(method))


def truncate(ranks, keep=KEEP):
    """Shortest prefix holding `keep` distinct services and at least `keep` entries."""
    services = []
    for index, name in enumerate(ranks, 1):
        service = to_service(name)
        if service not in services:
            services.append(service)
        if len(services) >= keep and index >= keep:
            return list(ranks[:index])
    return list(ranks)


def rank(method, frame, inject_time, dataset):
    """(ranks, reason); empty ranks with a reason mean the method abstains on this case."""
    normal = frame[frame["time"] < inject_time]
    anomalous = frame[frame["time"] >= inject_time]
    if normal.empty or anomalous.empty:
        return [], "no metric samples on one side of the inject time"
    result = upstream(method)(frame.reset_index(drop=True), inject_time=inject_time, dataset=dataset)
    ranks = [str(name) for name in result.get("ranks", [])]
    if not ranks:
        return [], "no metric changed in the window"
    return truncate(ranks), None


def predict(method, task_dir, task):
    """Ranks and usage for one blinded task, the same contract as the LLM methods."""
    frame = metric_frame(task_dir / task["files"]["metrics"], task)
    ranks, reason = rank(method, frame, task["inject_time"], str(task["dataset"]).lower())
    usage = {"metrics_in_window": int(frame.shape[1] - 1), "samples_in_window": int(frame.shape[0])}
    if reason:
        print("Abstained: " + reason, flush=True)
    return ranks, usage
