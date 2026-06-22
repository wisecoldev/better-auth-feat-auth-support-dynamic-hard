// @ts-nocheck
import { createAuthMiddleware } from "@better-auth/core/api";
import { describe, expect, it } from "vitest";
import { createAuthClient } from "../../client";
import { getTestInstance } from "../../test-utils/test-instance";

import { getTestCases } from "./validationParams";

describe("disallowed_host_without_fallback_throws", () => {
  const cases = getTestCases();
  it.each(cases)("case $#", async ({ inputs, expected }) => {
    const { auth } = await getTestInstance({
      baseURL: { allowedHosts: inputs.allowed_hosts },
    });

    let errorMessageLower = "<no_error>";
    try {
      await auth.handler(
        new Request("http://localhost:3000/api/auth/ok", {
          method: "GET",
          headers: {
            "x-forwarded-host": inputs.forwarded_host,
            "x-forwarded-proto": "https",
          },
        }),
      );
    } catch (e) {
      errorMessageLower = String((e as Error)?.message ?? "").toLowerCase();
    }

    const errorMessageLowerHostMatch =
      errorMessageLower.includes(inputs.forwarded_host.toLowerCase())
        ? inputs.forwarded_host.toLowerCase()
        : errorMessageLower;

    expect(errorMessageLowerHostMatch).toBe(
      expected.error_message_lower_host_match
    );
  }, 60_000);
});
