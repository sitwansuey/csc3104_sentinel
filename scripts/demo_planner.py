"""Key-free, cluster-free Planner demonstration using labelled fixtures."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.planner import propose, retrieve_runbooks, mock_provider

if __name__ == '__main__':
    incident = json.loads((ROOT / 'samples/messages/incident.json').read_text())
    baseline = json.loads((ROOT / 'samples/baseline.json').read_text())
    for baselines in ([baseline], []):
        print(json.dumps({'mode': 'offline_mock_provider', 'proposal':
            propose(incident, baselines, retrieve_runbooks, mock_provider)}, indent=2))
