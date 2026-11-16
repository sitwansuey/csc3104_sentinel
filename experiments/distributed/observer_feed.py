"""Observer process for a file-mode run: python -m experiments.distributed.observer_feed --config PATH.

It feeds a fixed, synthetic observation series (written by the topology)
through the real Observer state machine and outbox (runtime.observer_pipeline.ObserverStore)
and delivers the incident over HTTP(S) with the real RemoteIngress, retrying
until the Coordinator acknowledges it. Then it exits 0. The token comes from
the environment variable the configuration names, as for every worker.
"""
import argparse
import json
from pathlib import Path
import time

from runtime.observer_pipeline import ObserverStore
from runtime.remote_worker import RemoteIngress
from runtime.worker_service import client, read_json
from shared import log

LOG = log.get_logger("observer_feed")


@log.entrypoint("observer")
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--retry-seconds", type=float, default=0.5)
    parser.add_argument("--max-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    config = read_json(args.config)
    directory = Path(config["state_directory"])
    directory.mkdir(parents=True, exist_ok=True)
    policy, observations = read_json(config["policy"]), read_json(config["observations"])
    with ObserverStore(directory / "observer.db", policy, config["target"]) as store:
        if store.status()["polls"] == 0:  # A restarted feed never observes the series twice.
            for observation in observations:
                store.observe(observation)
        sink = RemoteIngress(client(config))
        deadline = time.monotonic() + args.max_seconds
        while True:
            result = store.flush(sink)
            print(json.dumps({"status": "flushed", **result}), flush=True)
            if result["error"] is None and not store.pending():
                log.emit(LOG, "observer.feed.delivered", status="delivered")
                return 0
            if time.monotonic() > deadline:
                return 1
            time.sleep(args.retry_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
