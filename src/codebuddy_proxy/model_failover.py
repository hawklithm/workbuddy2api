"""Observe routed responses without replaying an in-flight request."""
import json

from fastapi.responses import StreamingResponse

from codebuddy_proxy.model_routing import failover_state


def error_outcome(error, provider=None):
    if not isinstance(error, dict):
        return 'error'
    code = error.get('code')
    text = ' '.join(str(error.get(key, '')) for key in ('type', 'message')).lower()
    if (provider == 'trae' and code in (4008, 1005)) or any(word in text for word in ('insufficient_quota', 'quota exceeded', 'credits exhausted', '余额不足', '额度耗尽', '积分不足')):
        return 'quota'
    return 'error'


def observe_response(response, root, model, initial, target, options, recorder=None):
    def record(outcome):
        if recorder is not None:
            recorder(outcome)
        else:
            failover_state.record(root, model, initial, target, options, outcome)

    if not isinstance(response, StreamingResponse):
        status = response.status_code
        if status < 400:
            record('success')
        else:
            try:
                data = json.loads(response.body)
                error = data.get('error', data.get('detail', {}))
                if isinstance(error, dict) and isinstance(error.get('error'), dict):
                    error = error['error']
                outcome = error_outcome(error, target['provider'])
            except (ValueError, TypeError, AttributeError):
                outcome = 'error'
            if outcome == 'quota' or status in (401, 403, 429) or status >= 500:
                record(outcome)
        return response

    original = response.body_iterator
    async def observed():
        pending = ''
        outcome = 'success'
        completed = False
        recorded = False
        try:
            async for raw in original:
                text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
                pending += text
                lines = pending.split('\n')
                pending = lines.pop()[-65536:]
                for line in lines:
                    if not line.startswith('data:'):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except (ValueError, TypeError):
                        continue
                    if isinstance(data, dict):
                        error = data.get('error')
                        if not error and data.get('type') == 'response.failed':
                            error = (data.get('response') or {}).get('error') or {}
                        if error is not None:
                            found = error_outcome(error, target['provider'])
                            if outcome != 'quota':
                                outcome = found
                            if not recorded:
                                record(outcome)
                                recorded = True
                yield raw
            completed = True
        except Exception:
            if not recorded:
                record('error')
            raise
        finally:
            if completed and not recorded:
                record(outcome)
            close = getattr(original, 'aclose', None)
            if close is not None:
                await close()
    response.body_iterator = observed()
    return response
