#!/usr/bin/env python3
"""Exercise a real amd64 container against a local fake House, not a model eval."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'SYNTHETIC-CONTAINER-TEST-TOKEN'
GOOD = '''import json, os
from pathlib import Path
assert 'MODEL_TOKEN' not in os.environ
values=json.loads((Path(os.environ['TASK_DIR'])/'data.json').read_text())['values']
assert sum(values)==6
(Path(os.environ['OUT_DIR'])/'results.json').write_text(json.dumps({'total':sum(values)}))
'''
NUMERIC = '''import json, os
from pathlib import Path
import numpy as np, scipy.linalg, pandas as pd, pyarrow as pa
matrix=np.array([[2.,1.],[1.,2.]])
eigenvalues=scipy.linalg.eigvalsh(matrix)
assert np.allclose(eigenvalues,[1.,3.])
frame=pd.DataFrame({'x':[1,2,3]})
assert pa.Table.from_pandas(frame).num_rows == 3
(Path(os.environ['OUT_DIR'])/'results.json').write_text(json.dumps({'total':int(frame.x.sum())}))
'''


def run_case(image, mode, source):
    responses = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            last_message = body.get('messages', [{}])[-1].get('content', '')
            diagnostic = last_message.partition('Local diagnostic:\n')[2][:1500]
            responses.append({'path':self.path, 'model':body.get('model'), 'max_tokens':body.get('max_tokens'),
                              'synthetic_repair_diagnostic':diagnostic.replace(TOKEN,'[redacted]')})
            if self.path != '/v1/chat/completions' or self.headers.get('Authorization') != 'Bearer '+TOKEN:
                self.send_error(400)
                return
            code = NUMERIC if mode == 'numerics' else GOOD
            if mode == 'repair' and len(responses) == 1:
                code = 'raise ValueError("synthetic first candidate failed")'
            if mode == 'no-repair':
                code = 'raise ValueError("synthetic unrepairable failure")'
            if mode == 'timeout-budget':
                code = 'while True: pass'
            if mode == 'nonfinite-output':
                code = "import os\nfrom pathlib import Path\n(Path(os.environ['OUT_DIR'])/'results.json').write_text('{\"total\":NaN}')"
            if mode == 'network-isolation':
                code = '''import os,json
from pathlib import Path
socket=__import__('socket')
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise AssertionError('candidate could open network socket')
(Path(os.environ['OUT_DIR'])/'results.json').write_text(json.dumps({'total':6}))
'''
            if mode == 'read-isolation':
                code = '''import os, json
from pathlib import Path
try:
    Path('/input/checks/hidden.txt').read_text()
except PermissionError:
    pass
else:
    raise AssertionError('sealed checker was readable')
(Path(os.environ['OUT_DIR'])/'results.json').write_text(json.dumps({'total':6}))
'''
            if mode == 'write-isolation':
                code = '''import os,json
from pathlib import Path
try:
    Path('/input/data.json').write_text('bad')
except (PermissionError,OSError):
    pass
else:
    raise AssertionError('read-only input was writable')
(Path(os.environ['OUT_DIR'])/'results.json').write_text(json.dumps({'total':6}))
'''
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'code':code,'outputs':['results.json']})}}], 'usage':{'prompt_tokens':200,'completion_tokens':200}}).encode())

        def log_message(self,*args):
            pass

    http = ThreadingHTTPServer(('0.0.0.0',0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='quantguard-container-') as td:
            folder = Path(td).resolve()
            unit, output = folder/'input', folder/'output'
            unit.mkdir(); output.mkdir(); output.chmod(0o777)
            (unit/'checks').mkdir()
            (unit/'checks/hidden.txt').write_text('SYNTHETIC-CHECKER-DO-NOT-READ')
            (unit/'instruction.md').write_text('Sum data.json values and write /app/output/results.json with the total key.')
            (unit/'data.json').write_text('{"values":[1,2,3]}')
            (unit/'card.toml').write_text('[task]\nid="synthetic-contract"\ntrack="coding"\n[agent]\ntimeout_sec=45\n[environment]\nnetwork="restricted"\n')
            command = ['docker','run','--rm','--platform','linux/amd64','--read-only','--user','65534:65534',
                '--cap-drop=ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=64m',
                '--pids-limit','256','--ulimit','nofile=1024:1024','--ulimit','nproc=256:256',
                '--cpus','2','--memory','2g','--memory-swap','2g',
                '--mount',f'type=bind,src={unit},dst=/input,readonly',
                '--mount',f'type=bind,src={output},dst=/output',
                '--mount',f'type=bind,src={output},dst=/app/output',
                '-e',f'MODEL_ENDPOINT=http://host.docker.internal:{http.server_port}',
                '-e','MODEL_NAME=house','-e',f'MODEL_TOKEN={TOKEN}',
                '-e','QFBENCH_NETWORK=restricted','-e','QFBENCH_SEED=0','-e','QUANTGUARD_ISOLATION=container']
            if platform.system() == 'Linux':
                command += ['--add-host','host.docker.internal:host-gateway']
            if source:
                command += ['--mount',f'type=bind,src={ROOT / "src"},dst=/opt/quantguard/src,readonly',
                            '-e','PYTHONPATH=/opt/quantguard/src',image,'python','-m','quantguard.cli','solve']
            else:
                command += [image,'solve']
            command += ['--task-dir','/input','--out','/app/output','--wall-time','35',
                        '--candidate-timeout','10','--model-timeout','5']
            if mode in {'no-repair','nonfinite-output'}:
                command += ['--no-repair']
            if mode == 'timeout-budget':
                command += ['--candidate-timeout','0.3','--max-calls','1']
            completed = subprocess.run(command,capture_output=True,text=True,timeout=65)
            artifacts = {p.relative_to(output).as_posix():p.read_text(errors='replace')
                         for p in output.rglob('*') if p.is_file()}
            try:
                audit = json.loads(artifacts.get('quantguard-audit.json','{}'))
                result = json.loads(artifacts.get('results.json','null'))
            except ValueError:
                audit, result = {}, None
            expected_requests = 2 if mode=='repair' else 1
            passed = (completed.returncode==0 and result=={'total':6}
                      and audit.get('official_score') is None and len(responses)==expected_requests
                      and sorted(artifacts)==['quantguard-audit.json','results.json']
                      and TOKEN not in ''.join(artifacts.values())
                      and (unit/'data.json').read_text()=='{"values":[1,2,3]}')
            if mode in {'no-repair','timeout-budget','nonfinite-output'}:
                passed = (completed.returncode == 2 and result is None and audit.get('status') == 'failed'
                          and len(responses) == 1 and sorted(artifacts) == ['quantguard-audit.json']
                          and audit.get('official_score') is None and TOKEN not in ''.join(artifacts.values()))
            return {'case':mode,'passed':passed,'exit_code':completed.returncode,'requests':len(responses),
                    'audit':audit,'result':result,'synthetic_repair_diagnostics':[r['synthetic_repair_diagnostic'] for r in responses if r['synthetic_repair_diagnostic']],
                    'stderr':completed.stderr[-1000:].replace(TOKEN,'[redacted]'),
                    'stdout':completed.stdout[-2000:].replace(TOKEN,'[redacted]') if not passed else None}
    finally:
        http.shutdown();http.server_close();thread.join()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--image',default='microcat-quantguard:0.1.0')
    parser.add_argument('--source',action='store_true',help='Mount current source into an official Python base for bootstrap tests')
    parser.add_argument('--cases',default='success,repair,read-isolation,write-isolation,network-isolation,numerics,no-repair,timeout-budget,nonfinite-output')
    parser.add_argument('--report',type=Path,default=ROOT/'reports/container-smoke.json')
    args=parser.parse_args()
    results=[run_case(args.image,c,args.source) for c in args.cases.split(',')]
    image_id=subprocess.check_output(['docker','image','inspect',args.image,'--format','{{.Id}}'],text=True).strip()
    data={'checked_at':datetime.now(timezone.utc).isoformat(),'image':args.image,'image_id':image_id,
          'platform':'linux/amd64 on Apple Silicon emulation','scope':'Synthetic House HTTP responses; real container execution and structural checks; no real model or official task score',
          'official_score':None,'source_mounted':args.source,'cases':results,'passed':all(x['passed'] for x in results)}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps({'passed':data['passed'],'cases':[(r['case'],r['passed'],r['exit_code']) for r in results],
                      'report':str(args.report)},ensure_ascii=False))
    return 0 if data['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
