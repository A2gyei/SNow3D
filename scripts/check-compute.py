"""Run an end-to-end local API handler -> subprocess -> solver -> packer check.

No network listener is required. A real, independent 0.8 m run is retained.
"""
from io import BytesIO
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


class Probe(server.Handler):
    def __init__(self):
        self.path = '/api/run'
        payload = json.dumps({'thickness':.8, 'scenario':'null'}).encode()
        self.rfile = BytesIO(payload)
        self.headers = {'Host':'127.0.0.1:8765', 'Origin':'http://127.0.0.1:8765', 'Content-Length':str(len(payload))}
        self.result = None

    def json_response(self, data, status=200):
        self.result = status, data


if __name__ == '__main__':
    request = Probe()
    request.do_POST()
    assert request.result[0] == 202, request.result
    job_id = request.result[1]['id']
    print(f'Submitted real computation: {job_id}', flush=True)
    timeout = time.monotonic() + 600
    last_phase = None
    while time.monotonic() < timeout:
        path = ROOT / 'data' / 'jobs' / f'{job_id}.json'
        if path.exists():
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['phase'] != last_phase:
                print(json.dumps(job, ensure_ascii=False), flush=True)
                last_phase = job['phase']
            if job['phase'] == 'failed':
                raise RuntimeError(job['message'])
            if job['phase'] == 'complete':
                manifest = json.loads((ROOT / 'data/public' / job_id / 'manifest.json').read_text(encoding='utf-8'))
                assert manifest['initialThickness'] == .8
                assert len(manifest['frames']) > 3
                assert manifest['peakSpeed'] > 0
                print('FULL_COMPUTE_PIPELINE_OK', flush=True)
                break
        time.sleep(1)
    else:
        raise TimeoutError('Solver did not complete within 10 minutes')
