"""Opt-in real Boutique detection timing; creates/removes its dedicated lab."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test_boutique_live import BoutiqueExperiment, command
from scripts.test_executor_live import require, save, utc
from runtime.observer_pipeline import ObserverStore, LocalCoordinatorSink
from services.observer_kubernetes import CollectionError
from shared.contracts import validate_message


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def main():
    directory = ROOT / 'runs' / ('boutique-observer-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:6])
    lab = BoutiqueExperiment(directory)
    policy = {'detection': {'failure_duration_seconds': 5, 'max_sample_gap_seconds': 45}}
    report = {'status': 'RUNNING', 'scope': 'real Online Boutique v0.10.7 shippingservice detection only', 'policy': policy}
    created, template = False, None
    print('Observer acceptance evidence: ' + str(directory), flush=True)
    try:
        clusters = command(['kind', 'get', 'clusters']).split()
        require(lab.cluster_id not in clusters, 'Dedicated Boutique cluster already exists; inspect before running')
        created = True
        command(['kind', 'create', 'cluster', '--name', lab.cluster_id, '--config', 'infra/kind.yaml', '--wait', '120s'])
        lab.prepare_image()
        deployment = lab.deployment()
        template = deployment['spec']['template']
        lab.target = {'cluster_id': lab.cluster_id, 'namespace': lab.namespace, 'kind': 'Deployment',
                      'name': lab.name, 'uid': deployment['metadata']['uid']}
        save(directory / 'healthy-baseline.json', {'target': lab.target, 'deployment': deployment, 'captured_at': utc()})
        save(directory / 'policy.json', policy)
        collector = lab.observer_collector
        records = []
        def poll(label):
            observation = collector.collect()
            with ObserverStore(directory / 'observer.sqlite', policy, collector.target) as store:
                incident = store.observe(observation)
                require(store.flush(LocalCoordinatorSink(directory / 'coordinator.sqlite'))['error'] is None,
                        'Incident delivery failed')
                state = json.loads(store.db.execute('SELECT state FROM observer_checkpoint WHERE id=1').fetchone()[0])
            record = {'label': label, 'captured_at': observation['captured_at'],
                      'incident': incident, 'first_seen_at': state['episodes'][0]['first']['captured_at'] if state['episodes'] else None}
            records.append(record)
            save(directory / 'polls.json', records)
            save(directory / (label + '-observation.json'), observation)
            return record

        start = time.monotonic()
        while True:
            item = poll('healthy-' + str(len(records)))
            require(item['incident'] is None, 'Healthy workload produced an incident')
            current = lab.deployment()
            require(current.get('status', {}).get('readyReplicas') == 1 and current['status'].get('availableReplicas') == 1,
                    'Healthy observation lost desired readiness')
            if time.monotonic() - start >= 60:
                break
            time.sleep(2)
        report['healthy_seconds'] = time.monotonic() - start
        print('PASS: 60 seconds healthy without incidents', flush=True)
        lab.inject_fault()
        deadline = time.monotonic() + 150
        previous_elapsed = None
        while True:
            require(time.monotonic() < deadline, 'Detection timed out')
            try:
                item = poll('fault-' + str(len(records)))
            except CollectionError as error:
                with ObserverStore(directory / 'observer.sqlite', policy, collector.target) as store:
                    store.collection_failed(error)
                previous_elapsed = None
                time.sleep(2)
                continue
            elapsed = ((timestamp(item['captured_at']) - timestamp(item['first_seen_at'])).total_seconds()
                       if item['first_seen_at'] else None)
            if item['incident']:
                validate_message(item['incident'])
                require(item['incident']['target'] == lab.target, 'Wrong incident target')
                require(elapsed >= 5 and (previous_elapsed is None or previous_elapsed < 5),
                        'Incident did not occur on the first eligible threshold poll')
                report.update(incident=item['incident'], detection_elapsed_seconds=elapsed,
                              previous_poll_elapsed_seconds=previous_elapsed)
                break
            require(elapsed is None or elapsed < 5, 'Eligible poll failed to emit an incident')
            previous_elapsed = elapsed
            time.sleep(2)
        print('PASS: first eligible poll detected one incident', flush=True)
        start = time.monotonic()
        while True:
            require(poll('duplicate-' + str(len(records)))['incident'] is None, 'Duplicate incident emitted')
            if time.monotonic() - start >= 60:
                break
            time.sleep(2)
        report['duplicate_suppression_seconds'] = time.monotonic() - start
        require(lab.store.db.execute('SELECT count(*) FROM incidents').fetchone()[0] == 1, 'Expected one persisted incident')
        report['status'] = 'PASS'
        print('PASS: 60 further seconds without duplicate delivery', flush=True)
    except BaseException as error:
        report.update(status='FAILED', error=type(error).__name__ + ': ' + str(error)[:500])
        raise
    finally:
        try:
            if template:
                lab.kubectl('patch', 'deployment/' + lab.name, '--type=merge', '-p', json.dumps({'spec': {'template': template}}))
                lab.kubectl('rollout', 'status', 'deployment/' + lab.name, '--timeout=120s', timeout=160)
                report['restored_manually_by_harness'] = True
            if created:
                command(['kind', 'delete', 'cluster', '--name', lab.cluster_id])
                report['cluster_removed'] = lab.cluster_id not in command(['kind', 'get', 'clusters']).split()
        finally:
            lab.store.close()
            lab.credentials.unlink(missing_ok=True)
            save(directory / 'summary.json', report)


if __name__ == '__main__':
    main()
