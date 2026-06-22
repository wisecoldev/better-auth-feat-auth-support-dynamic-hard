// @ts-nocheck
import { createAuthMiddleware } from "@better-auth/core/api";
import { describe, expect, it } from "vitest";
import { createAuthClient } from "../../client";
import { getTestInstance } from "../../test-utils/test-instance";

import { getTestCases } from "./validationParams";

describe("empty_allowed_hosts_rejected", () => {
  const cases = getTestCases();
  it.each(cases)("case $#", async ({ inputs, expected }) => {
    let initError: unknown = null;
    let runtimeError: unknown = null;
    let instance: any = null;

    try {
      instance = await getTestInstance({
        baseURL: { allowedHosts: inputs.allowed_hosts } as any,
      });
    } catch (e) {
      initError = e;
    }

    if (!initError && instance) {
      try {
        await instance.auth.handler(
          new Request("http://localhost:3000/api/auth/ok", {
            method: "GET",
            headers: {
              "x-forwarded-host": inputs.followup_request_host,
              "x-forwarded-proto": "https",
            },
          }),
        );
      } catch (e) {
        runtimeError = e;
      }
    }

    const errorMessageLower = String(
      (initError as Error | null)?.message ??
      (runtimeError as Error | null)?.message ??
      "<no_error>"
    ).toLowerCase();

    const errorMessageLowerSubstringMatch = errorMessageLower.includes(inputs.required_substring)
      ? inputs.required_substring
      : errorMessageLower;

    expect(errorMessageLowerSubstringMatch).toBe(expected.error_message_lower_substring_match);
  }, 60_000);
});
