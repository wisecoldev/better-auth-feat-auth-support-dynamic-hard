## What the API looks like

`BetterAuthOptions.baseURL` accepts either:

- The existing static string, e.g. `"https://myapp.com"`.
- A dynamic config object:
  - `allowedHosts: string[]` — list of allowed host patterns.
    Wildcard patterns like `*.vercel.app`, `preview-*.myapp.com`, and
    `localhost:*` should match the corresponding hosts. The list must
    be non-empty (validate this — an empty list is a configuration
    error).
  - `fallback?: string` — a full URL used when an incoming request's
    host is not in `allowedHosts`. If not set, the request must be
    rejected.
  - `protocol?: "http" | "https" | "auto"` — explicit scheme.
    `"auto"` (or unset) means infer from `x-forwarded-proto` or the
    request URL.

Application authors will set this on `betterAuth({ baseURL: { ... } })`,
so the public type for `baseURL` must include this shape (and ideally
expose a named type for the dynamic-config object so consumers can
refer to it).

## What the request flow has to do

For each incoming request reaching `auth.handler(request)`:

1. Read the request's host from `x-forwarded-host` (proxy-aware
   deployments) or the `host` header.
2. Validate the host against `allowedHosts`, supporting wildcard
   patterns. The repo already has a wildcard utility at
   `packages/better-auth/src/utils/wildcard.ts` (already used by
   `trusted-origins.ts`) — lean on it rather than reimplementing.
3. If the host matches, the resolved origin for this request is
   `<protocol>://<host>` — protocol picked from the explicit
   `protocol` config, then `x-forwarded-proto`, then the request
   URL's protocol.
4. If the host doesn't match and `fallback` is configured, use the
   fallback as the resolved origin. If no fallback, reject the
   request with an error that names the rejected host.

The resolved origin must propagate to everything that reads
`options.baseURL` for THIS request: the auth context's `baseURL`
field, the JWT plugin's issuer/audience, the OIDC/MCP/OAuth-proxy URL
builders, the cookie domain (for cross-subdomain cookies), the
passkey rpID, telemetry — anywhere a string baseURL was being read
before. Find the call sites by grepping for the option name.

## Things to get right

- **Per-request, not per-init.** Each request can resolve to a
  different host. If you cache a single resolved baseURL on the
  shared `AuthContext` at construction time, multi-tenant requests
  collide.
- **Concurrent requests must not contaminate each other.** A naive
  implementation that mutates a shared `ctx.options.baseURL` will
  race when two different hosts hit `auth.handler` at the same time.
  Per-request isolation (a derived context, a clone, etc.) is the
  fix; do not mutate fields that vary per-request on the shared
  `AuthContext`.
- **`trustedOrigins` must auto-include `allowedHosts`.** Otherwise
  CSRF middleware blocks every request from a legitimate allowed
  host. Include each allowed host as `https://<host>`. Localhost
  entries should pick up both `http://` and `https://` variants —
  local dev typically runs on plain http and would otherwise be
  blocked. If `fallback` is set, its origin must also be in the
  trusted-origins list.
- **Static-string `baseURL` is unchanged.** The existing
  `baseURL: "https://..."` shape keeps working with no new behavior.
- **Cookies under `crossSubDomainCookies.enabled: true`.** When
  cross-subdomain cookies are on AND the baseURL is dynamic, the
  cookie's `Domain=` attribute must be recomputed per request to
  match that request's resolved host. With a static baseURL the
  existing init-time domain is correct as-is.
- **Pre-existing string consumers of `options.baseURL`.** Plugins
  (JWT, OIDC, MCP, OAuth-proxy), framework integrations
  (svelte-kit), packages (passkey, telemetry), and the cookie getter
  all currently treat `options.baseURL` as a string. After widening
  the type, every one of those consumers must safely handle the new
  union — never coerce the dynamic-config object into a string. The
  workspace TypeScript build is your safety net here; if it stays
  green, you've narrowed every site.

## Things you don't have to do

- Persistent per-tenant storage. The `allowedHosts` list is static
  at startup. The dynamism is per-request, not per-deployment-state.
- A breaking-change major version bump. The static-string config
  must keep working unchanged.
- Re-implementing wildcard matching from scratch — reuse the
  existing wildcard utility.
