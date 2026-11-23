"""DeepSeek Harness header integration stays scoped to the local proxy."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_dsh_session_header_hook():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required')
    script = r'''
import assert from 'node:assert/strict';
import { apply } from './integrations/deepseek-harness/workbuddy-session-header.mjs';
let handler;
apply({on(name, fn) { assert.equal(name, 'llm-pi-ai/request-headers'); handler = fn; }});
for (const [provider, baseURL, sessionId, expected] of [
  ['workbuddy','http://localhost:8787/v1','session-a','session-a'],
  ['workbuddy','http://127.0.0.1:8787/v1','session-b','session-b'],
  ['other','http://localhost:8787/v1','session-c',null],
  ['workbuddy','https://example.com/v1','session-d',null],
  ['workbuddy','http://localhost:8787/v1',undefined,null],
]) {
  const headers = new Headers();
  assert.equal(handler({provider, baseURL, sessionId}, headers, () => 'next'), 'next');
  assert.equal(headers.get('x-deepseek-harness-session-id'), expected);
}
const headers = new Headers({'x-deepseek-harness-session-id':'explicit'});
handler({provider:'workbuddy',baseURL:'http://localhost:8787/v1',sessionId:'other'},headers,()=>headers);
assert.equal(headers.get('x-deepseek-harness-session-id'), 'explicit');
'''
    subprocess.run([node, '--input-type=module', '-e', script], cwd=Path(__file__).parent, check=True)
