// Forward a session ID only to the locally configured workbuddy2api gateway.
export const name = 'workbuddy-session-header'

export function apply(ctx) {
  ctx.on('llm-pi-ai/request-headers', (request, headers, next) => {
    let endpoint
    try { endpoint = new URL(request.baseURL) } catch { return next() }
    if (request.provider === 'workbuddy' && request.sessionId
        && ['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname)
        && endpoint.port === '8787' && /^\/v1\/?$/.test(endpoint.pathname)
        && !headers.has('x-deepseek-harness-session-id')) {
      headers.set('x-deepseek-harness-session-id', String(request.sessionId))
    }
    return next()
  })
}
