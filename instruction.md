## Task

We need to handle deployments that serve multiple domains from the
same process. The motivating shape is a Vercel project running at
`myapp.com`, `www.myapp.com`, and a rotating set of preview URLs like
`feature-foo-abc123.vercel.app` simultaneously. Today Better Auth's
`baseURL` option is a single static string pinned at startup, so every
non-canonical-domain request gets cookies, OAuth callbacks, and CSRF
checks scoped to the wrong origin.

Extend `baseURL` so each request resolves its own origin, and every
pre-existing consumer of `options.baseURL` (cookies, trusted origins,
JWT issuer/audience, plugin URLs, telemetry) uses the resolved
per-request value. The existing static-string `baseURL` shape must
keep working with no behavior change.

## User stories

- Configuring the dynamic baseURL with an empty allowed-hosts list is treated as a configuration error. Either creating the auth instance or the very first request through it must surface an error whose message names the empty allowed-hosts field.
- A request that arrives via a proxy whose `x-forwarded-host` value matches an exact entry in the configured allow-list is treated as that host: the per-request baseURL exposed to the auth middleware is `https://<that-host>/api/auth`, with the scheme derived from `x-forwarded-proto`.
- A request whose host is not in the configured allow-list and where no fallback is configured is rejected by `auth.handler`. The rejection message must name the rejected host so operators can diagnose proxy / domain-config mismatches.
- Wildcard patterns in the allow-list match the corresponding hosts. A pattern like `*.vercel.app` matches any subdomain (`my-feature.vercel.app`, `preview-123.vercel.app`, etc.); a pattern like `preview-*.myapp.com` matches preview-prefixed subdomains.
- When a request's host is not in the allow-list AND a `fallback` URL is configured on the dynamic baseURL, the resolver uses the fallback as the per-request origin instead of rejecting.
- The optional `protocol` config on the dynamic baseURL takes precedence over the request's `x-forwarded-proto` header. When `protocol: "https"` is set, the resolved scheme is always `https://`, even if the proxy chain reports `http`.
- Configuring the dynamic baseURL automatically extends the `trustedOrigins` list with every entry in `allowedHosts`, so any allowed host is also a trusted origin. Each host appears as `https://<host>` in `trustedOrigins`. Localhost entries pick up both `https://` and `http://` variants since local development typically runs on plain HTTP. If `fallback` is set, its origin is also included.
- With cross-subdomain cookies enabled and a dynamic baseURL, two sequential requests from different allowed hosts produce different cookie `Domain=` attributes — each matching the request's resolved host.

## General instructions

- The code repo is at /repo/better-auth.
- You are inside of a Docker container. You may not be able to perform all operations you would normally be able to do on a local machine. Dependencies have not been pre-installed, and you may need to install them yourself.
- You are expected to act autonomously as a software engineer to complete tasks you are given.
- Do not stop until you feel you have completed the task and your code changes can be merged.
- You may need to use software engineering skills like analyzing the codebase, researching technologies, running services, analyzing logs, etc. to complete the task. Not all tasks will be solvable by reading source code alone.
