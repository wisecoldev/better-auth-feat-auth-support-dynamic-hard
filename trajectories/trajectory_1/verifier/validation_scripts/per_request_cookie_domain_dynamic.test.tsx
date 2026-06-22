// @ts-nocheck
import { createAuthMiddleware } from "@better-auth/core/api";
import { describe, expect, it } from "vitest";
import { createAuthClient } from "../../client";
import { getTestInstance } from "../../test-utils/test-instance";

import { getTestCases } from "./validationParams";

describe("per_request_cookie_domain_dynamic", () => {
  const cases = getTestCases();
  it.each(cases)("case $#", async ({ inputs, expected }) => {
    const capturedDomains: string[] = [];

    const { customFetchImpl } = await getTestInstance({
      baseURL: { allowedHosts: inputs.allowed_hosts, protocol: "https" } as any,
      advanced: {
        crossSubDomainCookies: { enabled: true },
      },
      hooks: {
        before: createAuthMiddleware(async (ctx) => {
          const dom = ctx.context.authCookies?.sessionToken?.attributes?.domain;
          if (typeof dom === "string") {
            capturedDomains.push(dom);
          }
        }),
      },
    });

    const client = createAuthClient({
      baseURL: "http://localhost:3000",
      fetchOptions: { customFetchImpl },
    });

    // First request
    await client.$fetch("/ok", {
      headers: {
        "x-forwarded-host": inputs.first_host,
        "x-forwarded-proto": "https",
      },
    });

    // Second request
    await client.$fetch("/ok", {
      headers: {
        "x-forwarded-host": inputs.second_host,
        "x-forwarded-proto": "https",
      },
    });

    const uniqueDomainsJson = JSON.stringify([...new Set(capturedDomains)].sort());

    expect(uniqueDomainsJson).toBe(expected.unique_domains_json);
  }, 60_000);
});
