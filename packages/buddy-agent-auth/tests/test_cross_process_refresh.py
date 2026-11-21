"""Cross-process refresh uses a single request and preserves rotated tokens."""
import json
import multiprocessing
from pathlib import Path
import time

from buddy_agent_auth import CodeBuddyClient


def _refresh_worker(session_path, counter_path, ready, go, results):
    client = CodeBuddyClient(session_file=Path(session_path))
    def request(*args, **kwargs):
        with open(counter_path, 'a', encoding='utf-8') as output:
            output.write('refresh\n')
            output.flush()
        time.sleep(0.4)
        return {'data': {'accessToken': 'new', 'refreshToken': 'rotated',
                         'expiresAt': int(time.time() * 1000) + 3600000}}
    client._request = request
    ready.put('ready')
    go.wait(10)
    try:
        results.put((client.refresh(), client.auth_headers().get('Authorization')))
    except Exception as exc:
        results.put(('error', type(exc).__name__))


def test_two_processes_refresh_once_and_reuse_rotated_token(tmp_path):
    session = tmp_path / 'session.json'
    client = CodeBuddyClient(session_file=session)
    client._save_session({'auth': {'accessToken': 'old', 'refreshToken': 'old-refresh', 'expiresAt': 1},
                          'account': {'uid': 'synthetic'}, 'machineId': 'machine'})
    counter = tmp_path / 'calls.txt'
    ctx = multiprocessing.get_context('spawn')
    ready, results, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    workers = [ctx.Process(target=_refresh_worker, args=(str(session), str(counter), ready, go, results))
               for _ in range(2)]
    for process in workers:
        process.start()
    try:
        assert ready.get(timeout=15) == 'ready'
        assert ready.get(timeout=15) == 'ready'
        go.set()
        outcomes = [results.get(timeout=15), results.get(timeout=15)]
        assert all(ok is True and token == 'Bearer new' for ok, token in outcomes), outcomes
        assert counter.read_text().splitlines() == ['refresh']
        saved = json.loads(session.read_text(encoding='utf-8'))
        assert saved['auth']['refreshToken'] == 'rotated'
    finally:
        for process in workers:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
