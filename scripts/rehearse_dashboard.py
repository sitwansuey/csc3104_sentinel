"""Local read-only dashboard rehearsal with explicit collector/API outage controls.

No evidence is fabricated. A local control file pauses reads or makes the API
return 503 so the real frontend can be inspected. It never mutates a workload.
"""
import argparse
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.cluster_metrics import collect_cluster_snapshot
from runtime.dashboard import make_server, snapshot
from runtime.telemetry import emit_cluster_snapshot
from scripts.dashboard import ensure_frontend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--cluster-id', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--port', type=int, default=8775)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    if not (directory / 'coordinator.sqlite').is_file():
        parser.error('Use an existing real demo run with a Coordinator database')
    control = directory / 'dashboard-rehearsal-control.txt'
    control.write_text('live', encoding='utf-8')
    journal = directory / 'dashboard-rehearsal.sqlite'
    stop = threading.Event()
    def mode():
        value = control.read_text(encoding='utf-8-sig').strip()
        if value not in {'live', 'paused', 'unavailable'}:
            raise ValueError('Control must be live, paused or unavailable')
        return value
    def collect():
        while not stop.is_set():
            try:
                if mode() == 'live':
                    emit_cluster_snapshot(journal, collect_cluster_snapshot(
                        context=args.context, cluster_id=args.cluster_id, namespace=args.namespace))
            except (ValueError, OSError, RuntimeError):
                pass
            stop.wait(2)
    def read():
        if mode() == 'unavailable':
            raise OSError('Explicit rehearsal API outage')
        data = snapshot(directory / 'coordinator.sqlite', journal)
        (directory / 'dashboard-rehearsal-last.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
        return data
    ensure_frontend()
    server = make_server(directory / 'coordinator.sqlite', journal, port=args.port, provider=read)
    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    print(f'Read-only rehearsal: http://127.0.0.1:{server.server_port}', flush=True)
    print(f'Local control file: {control} (live / paused / unavailable)', flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=35)
        if thread.is_alive():
            raise RuntimeError('Collector did not stop within its bounded request window')


if __name__ == '__main__':
    main()
