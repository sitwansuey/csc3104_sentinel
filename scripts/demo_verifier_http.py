"""Real local HTTP evidence, explicitly simulated Kubernetes and execution."""

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.verifier.live import LiveEvidenceCollector, collect_window


class DemoKubernetes:
    def get(self, path):
        name = path.rsplit("/", 1)[-1]
        return {"kind": "Deployment", "metadata": {"name": name, "namespace": "demo", "uid": name + "-demo-uid",
                "generation": 1, "resourceVersion": "simulated-version"}, "spec": {"replicas": 1},
                "status": {"observedGeneration": 1, "readyReplicas": 1, "availableReplicas": 1, "updatedReplicas": 1}}


def main():
    statuses = {"/checkout": 200, "/payment": 200}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(statuses.get(self.path, 404))
            self.end_headers()
            self.wfile.write(b'{"health":"demonstration"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {"cluster_id": "simulated-cluster", "dependency_inventory_complete": True,
              "declared_edges": [{"from": "checkout", "to": "payment"}], "services": {}}
    for name in ("checkout", "payment"):
        config["services"][name] = {"target": {"cluster_id": "simulated-cluster", "namespace": "demo",
            "kind": "Deployment", "name": name, "uid": name + "-demo-uid"},
            "health_url": f"http://127.0.0.1:{server.server_port}/{name}"}
    execution = {"schema_version": "1.0", "kind": "execution_result", "incident_id": "HTTP-DEMO",
                 "action_id": "HTTP-DEMO-ACTION", "target": config["services"]["checkout"]["target"],
                 "status": "APPLIED", "resource_version": "simulated-version",
                 "recorded_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                 "details": "Synthetic execution; no recovery action was performed"}
    policy = {"health": {"minimum_samples": 3, "stable_window_seconds": 1,
                         "max_sample_gap_seconds": 2, "required_http_status": 200}}
    results = {}
    try:
        for scenario in ("healthy", "service_failure", "dependency_failure", "unknown_inventory"):
            statuses.update({"/checkout": 503 if scenario == "service_failure" else 200,
                             "/payment": 503 if scenario == "dependency_failure" else 200})
            config["dependency_inventory_complete"] = scenario != "unknown_inventory"
            result = collect_window(LiveEvidenceCollector(config, DemoKubernetes()), execution,
                                    policy, count=3, interval_seconds=0.5)
            results[scenario] = {"status": result["outcome"]["status"],
                                 "readings": len(result["samples"]),
                                 "window_start": result["outcome"]["window_start"],
                                 "window_end": result["outcome"]["window_end"]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print(json.dumps({"mode": "real_http_simulated_kubernetes", "execution": "synthetic",
                      "traces": "not configured; declared demonstration graph", "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
