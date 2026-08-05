import test from "node:test";
import assert from "node:assert/strict";

import worker from "../src/index.js";

const encoder = new TextEncoder();
const CONTROL_HOST = "mim.madup.app";
const APP_HOST = "sample-a1b2c3d4.madup.app";
const APP_SUFFIX = "madup.app";
const PROJECT_NUMBER = "123456789012";
const CONTROL_ORIGIN = `https://mim-control-plane-${PROJECT_NUMBER}.asia-northeast3.run.app`;
const APP_ORIGIN = `https://mim-app-gateway-${PROJECT_NUMBER}.asia-northeast3.run.app`;
const CONTROL_SECRET = "0123456789abcdef0123456789abcdef";
const APP_SECRET = "fedcba9876543210fedcba9876543210";
const CONTROL_MAX_BODY_BYTES = 16 * 1024;
const APP_MAX_BODY_BYTES = 1024 * 1024;

function createEnv(overrides = {}) {
  return {
    MIM_CONTROL_PUBLIC_HOSTNAME: CONTROL_HOST,
    MIM_APP_HOST_SUFFIX: APP_SUFFIX,
    MIM_PROJECT_NUMBER: PROJECT_NUMBER,
    MIM_CONTROL_ORIGIN: CONTROL_ORIGIN,
    MIM_APP_GATEWAY_ORIGIN: APP_ORIGIN,
    MIM_CONTROL_ALLOWED_ROUTES: [
      "POST /mcp",
      "GET /healthz",
      "GET /readyz",
      "GET /mcp/ws",
      "GET /v1/operations/{id}",
      "GET /v1/plan/deploy?*",
      "POST /v1/webhooks/github",
    ].join("\n"),
    MIM_CONTROL_ORIGIN_HMAC_KEY_ID: "control-current",
    MIM_APP_GATEWAY_ORIGIN_HMAC_KEY_ID: "app-current",
    MIM_CONTROL_ORIGIN_HMAC_SECRET: CONTROL_SECRET,
    MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET: APP_SECRET,
    ...overrides,
  };
}

function accessHeaders(overrides = {}) {
  return {
    "cf-access-jwt-assertion": "user-access-jwt",
    ...overrides,
  };
}

function makeRequest(host, path, { method = "GET", headers = {}, body } = {}) {
  const init = { method, headers, body };
  if (body instanceof ReadableStream) {
    init.duplex = "half";
  }
  return new Request(`https://${host}${path}`, init);
}

function makeRawRequest({ url, method = "GET", headers = {}, body = null }) {
  return {
    url,
    method,
    headers: headers instanceof Headers ? headers : new Headers(headers),
    body,
  };
}

function streamFromChunks(chunks) {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

function erroringStream(message = "stream read failed") {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode("partial"));
      controller.error(new Error(message));
    },
  });
}

async function sha256Hex(value) {
  const bytes =
    value instanceof Uint8Array
      ? value
      : typeof value === "string"
        ? encoder.encode(value)
        : new Uint8Array(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Buffer.from(digest).toString("hex");
}

async function signCanonicalMessage({
  secret,
  destinationClass,
  method,
  publicHost,
  path,
  body,
  timestamp,
  requestId,
  keyId,
}) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const message = [
    "mim-origin-v2",
    destinationClass,
    method.toUpperCase(),
    publicHost.toLowerCase(),
    path,
    await sha256Hex(body),
    String(timestamp),
    requestId,
    keyId,
  ].join("\n");
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return Buffer.from(signature).toString("hex");
}

async function withDeterministicRequestIds(run) {
  const originalNow = Date.now;
  const originalRandomUuid = crypto.randomUUID;
  Date.now = () => 1785859200000;
  crypto.randomUUID = () => "11111111-2222-4333-8444-555555555555";
  try {
    await run();
  } finally {
    Date.now = originalNow;
    crypto.randomUUID = originalRandomUuid;
  }
}

test("routes only the exact control host to the control origin and emits v2 proof headers", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({
      url: request.url,
      method: request.method,
      headers: Object.fromEntries(request.headers.entries()),
      bodyText: await request.text(),
    });
    return new Response("ok", {
      status: 200,
      headers: {
        "set-cookie": "session=control; Path=/; Domain=.madup.app; HttpOnly",
      },
    });
  };

  try {
    await withDeterministicRequestIds(async () => {
      const response = await worker.fetch(
        makeRequest(CONTROL_HOST, "/mcp", {
          method: "POST",
          headers: {
            ...accessHeaders({
              authorization: "Bearer caller-secret",
              "proxy-authorization": "Basic caller-secret",
              cookie: "session=keep; CF_Authorization=drop; __Secure-mim_session=drop",
              forwarded: "for=203.0.113.10;host=evil.example",
              "x-forwarded-for": "203.0.113.10",
              "x-mim-origin-signature": "forged",
              "cf-access-authenticated-user-email": "person@madup.com",
            }),
          },
          body: '{"action":"status"}',
        }),
        createEnv(),
        {},
      );

      assert.equal(response.status, 200);
      assert.deepEqual(response.headers.getSetCookie(), [
        "session=control; Path=/; HttpOnly",
      ]);
    });

    assert.equal(calls.length, 1);
    const forwarded = calls[0];
    assert.equal(forwarded.url, `${CONTROL_ORIGIN}/mcp`);
    assert.equal(forwarded.method, "POST");
    assert.equal(forwarded.headers["cf-access-jwt-assertion"], "user-access-jwt");
    assert.equal(forwarded.headers.authorization, undefined);
    assert.equal(forwarded.headers["proxy-authorization"], undefined);
    assert.equal(forwarded.headers.forwarded, undefined);
    assert.equal(forwarded.headers["x-forwarded-for"], undefined);
    assert.equal(forwarded.headers["cf-access-authenticated-user-email"], undefined);
    assert.equal(forwarded.headers.cookie, "session=keep");
    assert.equal(forwarded.headers["x-mim-origin-key-id"], "control-current");
    assert.equal(forwarded.headers["x-mim-origin-request-id"], "11111111-2222-4333-8444-555555555555");
    assert.equal(forwarded.headers["x-mim-origin-public-host"], CONTROL_HOST);
    assert.equal(forwarded.headers["x-mim-origin-destination-class"], "control-plane");

    assert.equal(
      forwarded.headers["x-mim-origin-signature"],
      "c21bbf52302fd8c27a309e922b48b9339ffaeebefe60e12e349c6ae9e3d7a1ed",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("routes single-label app hosts only to the app gateway and preserves app-local cookies", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({
      url: request.url,
      method: request.method,
      headers: Object.fromEntries(request.headers.entries()),
    });
    return new Response("app-ok", {
      status: 200,
      headers: [
        ["set-cookie", "app_session=1; Path=/; Domain=.madup.app; Secure; HttpOnly"],
        ["set-cookie", "run_cookie=1; Path=/; Domain=internal-service.a.run.app; Secure"],
      ],
    });
  };

  try {
    await withDeterministicRequestIds(async () => {
      const response = await worker.fetch(
        makeRequest(APP_HOST, "/dashboard?tab=overview", {
          method: "GET",
          headers: accessHeaders({
            cookie: "app_session=keep; theme=dark; CF_Authorization=drop; mim_session=drop",
            authorization: "Bearer app-secret",
            "x-forwarded-host": "evil.example",
            "x-mim-origin-key-id": "forged",
          }),
        }),
        createEnv(),
        {},
      );

      assert.equal(response.status, 200);
      assert.equal(await response.text(), "app-ok");
      assert.deepEqual(response.headers.getSetCookie(), [
        "app_session=1; Path=/; Secure; HttpOnly",
        "run_cookie=1; Path=/; Secure",
      ]);
    });

    assert.equal(calls.length, 1);
    const forwarded = calls[0];
    assert.equal(forwarded.url, `${APP_ORIGIN}/dashboard?tab=overview`);
    assert.equal(forwarded.method, "GET");
    assert.equal(forwarded.headers["cf-access-jwt-assertion"], "user-access-jwt");
    assert.equal(forwarded.headers.authorization, undefined);
    assert.equal(forwarded.headers["x-forwarded-host"], undefined);
    assert.equal(forwarded.headers.cookie, "app_session=keep; theme=dark");
    assert.equal(forwarded.headers["x-mim-origin-key-id"], "app-current");
    assert.equal(forwarded.headers["x-mim-origin-public-host"], APP_HOST);
    assert.equal(forwarded.headers["x-mim-origin-destination-class"], "app-gateway");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("strips caller-controlled cf and transport metadata while preserving ordinary safe headers", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({
      url: request.url,
      headers: Object.fromEntries(request.headers.entries()),
    });
    return new Response("ok", { status: 200 });
  };

  try {
    const controlResponse = await worker.fetch(
      makeRequest(CONTROL_HOST, "/healthz", {
        method: "GET",
        headers: accessHeaders({
          "cf-ray": "1234abcd",
          "cf-connecting-ip": "203.0.113.10",
          "cf-ipcountry": "KR",
          "cf-worker": "spoofed",
          "true-client-ip": "203.0.113.11",
          "x-real-ip": "203.0.113.12",
          "cdn-loop": "cloudflare",
          "accept-language": "ko-KR",
          "if-none-match": '"etag-1"',
          "user-agent": "Mozilla/5.0",
        }),
      }),
      createEnv(),
      {},
    );
    assert.equal(controlResponse.status, 200);

    const appResponse = await worker.fetch(
      makeRequest(APP_HOST, "/dashboard", {
        method: "GET",
        headers: accessHeaders({
          "cf-ray": "5678efgh",
          "cf-connecting-ip": "198.51.100.20",
          "cf-ipcountry": "US",
          "true-client-ip": "198.51.100.21",
          "x-real-ip": "198.51.100.22",
          "cdn-loop": "cloudflare",
          "accept-language": "en-US",
          pragma: "no-cache",
          "user-agent": "Mozilla/5.0",
        }),
      }),
      createEnv(),
      {},
    );
    assert.equal(appResponse.status, 200);

    assert.equal(calls.length, 2);
    for (const call of calls) {
      assert.equal(call.headers["cf-ray"], undefined);
      assert.equal(call.headers["cf-connecting-ip"], undefined);
      assert.equal(call.headers["cf-ipcountry"], undefined);
      assert.equal(call.headers["cf-worker"], undefined);
      assert.equal(call.headers["true-client-ip"], undefined);
      assert.equal(call.headers["x-real-ip"], undefined);
      assert.equal(call.headers["cdn-loop"], undefined);
    }
    assert.equal(calls[0].headers["accept-language"], "ko-KR");
    assert.equal(calls[0].headers["if-none-match"], '"etag-1"');
    assert.equal(calls[0].headers["user-agent"], "Mozilla/5.0");
    assert.equal(calls[1].headers["accept-language"], "en-US");
    assert.equal(calls[1].headers["pragma"], "no-cache");
    assert.equal(calls[1].headers["user-agent"], "Mozilla/5.0");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("allows well-formed percent-encoded app targets and binds the exact escaped path and query into the HMAC", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({
      url: request.url,
      headers: Object.fromEntries(request.headers.entries()),
    });
    return new Response("ok", { status: 200 });
  };

  try {
    await withDeterministicRequestIds(async () => {
      const response = await worker.fetch(
        makeRequest(APP_HOST, "/streamlit/%E2%9C%93?tab=hello%20world", {
          method: "GET",
          headers: accessHeaders({
            "accept-language": "en-US",
          }),
        }),
        createEnv(),
        {},
      );

      assert.equal(response.status, 200);
    });

    assert.equal(calls.length, 1);
    assert.equal(
      calls[0].url,
      `${APP_ORIGIN}/streamlit/%E2%9C%93?tab=hello%20world`,
    );
    assert.equal(calls[0].headers["x-mim-origin-signature"], "2ca4f2f7c146f6e068d7afdc8172ef86467ffe2c890954e5e422a5e004451d55");
    assert.equal(calls[0].headers["x-mim-origin-public-host"], APP_HOST);
    assert.equal(calls[0].headers["x-mim-origin-destination-class"], "app-gateway");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("drops sensitive credential cookies in both request Cookie and origin Set-Cookie surfaces", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({
      headers: Object.fromEntries(request.headers.entries()),
    });
    return new Response("ok", {
      status: 200,
      headers: [
        ["set-cookie", "CF_Authorization=edge-secret; Path=/; HttpOnly"],
        ["set-cookie", "__Secure-CF_Authorization=edge-secret; Path=/; HttpOnly"],
        ["set-cookie", "mim_session=edge-secret; Path=/; HttpOnly"],
        ["set-cookie", "mim-session=edge-secret; Path=/; HttpOnly"],
        ["set-cookie", "__Host-mim_session=edge-secret; Path=/; HttpOnly"],
        ["set-cookie", "__Secure-mim_session=edge-secret; Path=/; HttpOnly"],
        ["set-cookie", "theme=dark; Path=/; Domain=.madup.app"],
        ["set-cookie", "app_session=1; Path=/; Domain=.run.app; Secure; HttpOnly"],
      ],
    });
  };

  try {
    const response = await worker.fetch(
      makeRequest(APP_HOST, "/dashboard", {
        method: "GET",
        headers: accessHeaders({
          cookie: [
            "CF_Authorization=drop",
            "__Secure-CF_Authorization=drop",
            "mim_session=drop",
            "mim-session=drop",
            "__Host-mim_session=drop",
            "__Secure-mim_session=drop",
            "app_session=keep",
            "theme=dark",
          ].join("; "),
        }),
      }),
      createEnv(),
      {},
    );

    assert.equal(response.status, 200);
    assert.deepEqual(response.headers.getSetCookie(), [
      "theme=dark; Path=/",
      "app_session=1; Path=/; Secure; HttpOnly",
    ]);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].headers.cookie, "app_session=keep; theme=dark");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("rejects apex, nested, punycode, alternate suffix, reserved, port, and credential-bearing hosts", async () => {
  let called = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    called = true;
    return new Response("unexpected");
  };

  const deniedRequests = [
    makeRequest("madup.app", "/"),
    makeRequest("nested.slug.madup.app", "/"),
    makeRequest("xn--bcher-kva.madup.app", "/"),
    makeRequest("sample-a1b2c3d4.example.com", "/"),
    makeRequest("www.madup.app", "/"),
    makeRawRequest({ url: "https://sample-a1b2c3d4.madup.app:8443/" }),
    makeRawRequest({ url: "https://user:pass@sample-a1b2c3d4.madup.app/" }),
  ];

  try {
    for (const request of deniedRequests) {
      const response = await worker.fetch(request, createEnv(), {});
      assert.equal(response.status, 403);
      assert.equal(await response.text(), "Request denied.");
    }
    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("denies malformed percent escapes and fragments on app hosts while keeping control-plane percent denial", async () => {
  let called = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    called = true;
    return new Response("unexpected");
  };

  const deniedAppRequests = [
    makeRawRequest({
      url: `https://${APP_HOST}/bad/%ZZ`,
      method: "GET",
      headers: accessHeaders(),
    }),
    makeRawRequest({
      url: `https://${APP_HOST}/bad/%`,
      method: "GET",
      headers: accessHeaders(),
    }),
    makeRawRequest({
      url: `https://${APP_HOST}/bad/%2`,
      method: "GET",
      headers: accessHeaders(),
    }),
    makeRawRequest({
      url: `https://${APP_HOST}/bad/%E2%9C%93?tab=%`,
      method: "GET",
      headers: accessHeaders(),
    }),
    makeRawRequest({
      url: `https://${APP_HOST}/bad#fragment`,
      method: "GET",
      headers: accessHeaders(),
    }),
  ];

  try {
    for (const request of deniedAppRequests) {
      const response = await worker.fetch(request, createEnv(), {});
      assert.equal(response.status, 404);
      assert.equal(await response.text(), "Route not available.");
    }

    const controlPercentDenied = await worker.fetch(
      makeRawRequest({
        url: `https://${CONTROL_HOST}/mcp%20`,
        method: "POST",
        headers: accessHeaders(),
        body: "{}",
      }),
      createEnv(),
      {},
    );
    assert.equal(controlPercentDenied.status, 404);
    assert.equal(await controlPercentDenied.text(), "Route not available.");

    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("requires exactly one non-empty Access assertion for every app route", async () => {
  let called = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    called = true;
    return new Response("unexpected");
  };

  const invalidHeaders = [
    {},
    { "cf-access-jwt-assertion": "" },
    [
      ["cf-access-jwt-assertion", "token-a"],
      ["cf-access-jwt-assertion", "token-b"],
    ],
  ];

  try {
    for (const headers of invalidHeaders) {
      const response = await worker.fetch(
        makeRequest(APP_HOST, "/dashboard", { headers }),
        createEnv(),
        {},
      );
      assert.equal(response.status, 403);
      assert.equal(await response.text(), "Request denied.");
    }
    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("keeps the GitHub webhook bypass exact on the control host", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({
      url: request.url,
      headers: Object.fromEntries(request.headers.entries()),
      bodyText: await request.text(),
    });
    return new Response("ok", { status: 200 });
  };

  try {
    const allowed = await worker.fetch(
      makeRequest(CONTROL_HOST, "/v1/webhooks/github", {
        method: "POST",
        headers: {
          "x-hub-signature-256": "sha256=" + "a".repeat(64),
          "x-github-event": "push",
          "x-github-delivery": "11111111-2222-4333-8444-555555555555",
          cookie: "session=leak-me",
          authorization: "Bearer leak-me",
          "content-type": "application/json",
          "user-agent": "GitHub-Hookshot/test",
          "x-extra-header": "leak-me",
        },
        body: '{"zen":"edge"}',
      }),
      createEnv(),
      {},
    );
    assert.equal(allowed.status, 200);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, `${CONTROL_ORIGIN}/v1/webhooks/github`);
    assert.equal(calls[0].headers["cf-access-jwt-assertion"], undefined);
    assert.equal(calls[0].headers["x-hub-signature-256"], "sha256=" + "a".repeat(64));
    assert.equal(calls[0].headers.cookie, undefined);
    assert.equal(calls[0].headers.authorization, undefined);
    assert.equal(calls[0].headers["content-type"], undefined);
    assert.equal(calls[0].headers["user-agent"], undefined);
    assert.equal(calls[0].headers["x-extra-header"], undefined);
    assert.equal(calls[0].headers["x-mim-origin-key-id"], "control-current");
    assert.equal(calls[0].headers["x-mim-origin-destination-class"], "control-plane");

    const wrongMethod = await worker.fetch(
      makeRequest(CONTROL_HOST, "/v1/webhooks/github", {
        method: "GET",
        headers: accessHeaders(),
      }),
      createEnv(),
      {},
    );
    assert.equal(wrongMethod.status, 405);

    const extraPath = await worker.fetch(
      makeRequest(CONTROL_HOST, "/v1/webhooks/github/extra", {
        method: "POST",
        headers: {
          "x-hub-signature-256": "sha256=" + "a".repeat(64),
          "x-github-event": "push",
          "x-github-delivery": "11111111-2222-4333-8444-555555555555",
        },
        body: "{}",
      }),
      createEnv(),
      {},
    );
    assert.equal(extraPath.status, 404);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("allows reviewed app methods and denies CONNECT and TRACE", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({ url: request.url, method: request.method });
    return new Response(null, { status: 204 });
  };

  try {
    for (const method of ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]) {
      const response = await worker.fetch(
        makeRequest(APP_HOST, "/app", {
          method,
          headers: accessHeaders(),
          body: method === "POST" || method === "PUT" || method === "PATCH" ? "{}" : undefined,
        }),
        createEnv(),
        {},
      );
      assert.equal(response.status, 204);
    }

    for (const method of ["CONNECT", "TRACE"]) {
      const response = await worker.fetch(
        makeRawRequest({
          url: `https://${APP_HOST}/app`,
          method,
          headers: accessHeaders(),
        }),
        createEnv(),
        {},
      );
      assert.equal(response.status, 405);
    }

    assert.deepEqual(
      calls.map((call) => call.method),
      ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("rejects control-host routes outside the explicit control allowlist", async () => {
  let called = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    called = true;
    return new Response("unexpected");
  };

  try {
    const wrongMethod = await worker.fetch(
      makeRequest(CONTROL_HOST, "/mcp", {
        method: "DELETE",
        headers: accessHeaders(),
      }),
      createEnv(),
      {},
    );
    assert.equal(wrongMethod.status, 405);

    const wrongPath = await worker.fetch(
      makeRequest(CONTROL_HOST, "/admin", {
        method: "POST",
        headers: accessHeaders(),
      }),
      createEnv(),
      {},
    );
    assert.equal(wrongPath.status, 404);
    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("rejects request bodies above the reviewed size limit before origin fetch", async () => {
  let called = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    called = true;
    return new Response("unexpected");
  };

  try {
    const controlTooLarge = await worker.fetch(
      makeRequest(CONTROL_HOST, "/mcp", {
        method: "POST",
        headers: accessHeaders(),
        body: "x".repeat(CONTROL_MAX_BODY_BYTES + 1),
      }),
      createEnv(),
      {},
    );
    assert.equal(controlTooLarge.status, 413);
    assert.equal(await controlTooLarge.text(), "Payload too large.");

    const appAtLimit = await worker.fetch(
      makeRequest(APP_HOST, "/upload", {
        method: "POST",
        headers: accessHeaders(),
        body: "x".repeat(APP_MAX_BODY_BYTES),
      }),
      createEnv(),
      {},
    );
    assert.equal(appAtLimit.status, 200);

    const appTooLarge = await worker.fetch(
      makeRequest(APP_HOST, "/upload", {
        method: "POST",
        headers: accessHeaders(),
        body: "x".repeat(APP_MAX_BODY_BYTES + 1),
      }),
      createEnv(),
      {},
    );

    assert.equal(appTooLarge.status, 413);
    assert.equal(await appTooLarge.text(), "Payload too large.");
    assert.equal(called, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("returns 400 when reading a streamed request body fails", async () => {
  let called = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    called = true;
    return new Response("unexpected");
  };

  try {
    const response = await worker.fetch(
      makeRequest(APP_HOST, "/upload", {
        method: "POST",
        headers: accessHeaders(),
        body: erroringStream(),
      }),
      createEnv(),
      {},
    );

    assert.equal(response.status, 400);
    assert.equal(await response.text(), "Request could not be processed.");
    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("forwards streamed request bodies unchanged", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({
      bodyText: await request.text(),
    });
    return new Response("ok", { status: 200 });
  };

  try {
    const response = await worker.fetch(
      makeRequest(APP_HOST, "/upload", {
        method: "POST",
        headers: accessHeaders(),
        body: streamFromChunks(['{"chunk":1,', '"more":true}']),
      }),
      createEnv(),
      {},
    );

    assert.equal(response.status, 200);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].bodyText, '{"chunk":1,"more":true}');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("allows streamed app request bodies up to exactly 1 MiB and rejects 1 MiB plus one byte before origin fetch", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({
      bodyText: await request.text(),
    });
    return new Response("ok", { status: 200 });
  };

  try {
    const atLimitBody = "a".repeat(APP_MAX_BODY_BYTES);
    const atLimit = await worker.fetch(
      makeRequest(APP_HOST, "/upload", {
        method: "POST",
        headers: accessHeaders(),
        body: streamFromChunks([atLimitBody]),
      }),
      createEnv(),
      {},
    );
    assert.equal(atLimit.status, 200);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].bodyText.length, APP_MAX_BODY_BYTES);

    const overLimitBody = ["a".repeat(APP_MAX_BODY_BYTES), "b"];
    const overLimit = await worker.fetch(
      makeRequest(APP_HOST, "/upload", {
        method: "POST",
        headers: accessHeaders(),
        body: streamFromChunks(overLimitBody),
      }),
      createEnv(),
      {},
    );
    assert.equal(overLimit.status, 413);
    assert.equal(await overLimit.text(), "Payload too large.");
    assert.equal(calls.length, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("preserves query-wildcard and template control routes from the reviewed allowlist", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({ url: request.url, method: request.method });
    return new Response("ok", { status: 200 });
  };

  try {
    const queryWildcard = await worker.fetch(
      makeRequest(CONTROL_HOST, "/v1/plan/deploy?plan_id=plan-1&plan_hash=hash-1&idempotency_key=idem-1", {
        method: "GET",
        headers: accessHeaders(),
      }),
      createEnv(),
      {},
    );
    assert.equal(queryWildcard.status, 200);

    const templateRoute = await worker.fetch(
      makeRequest(CONTROL_HOST, "/v1/operations/op-123", {
        method: "GET",
        headers: accessHeaders(),
      }),
      createEnv(),
      {},
    );
    assert.equal(templateRoute.status, 200);

    assert.deepEqual(calls, [
      {
        url: `${CONTROL_ORIGIN}/v1/plan/deploy?plan_id=plan-1&plan_hash=hash-1&idempotency_key=idem-1`,
        method: "GET",
      },
      {
        url: `${CONTROL_ORIGIN}/v1/operations/op-123`,
        method: "GET",
      },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("preserves websocket upgrade headers and streaming responses for app traffic", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push({
      headers: Object.fromEntries(request.headers.entries()),
    });
    return new Response(streamFromChunks(["stream-", "ok"]), {
      status: 200,
      headers: { "content-type": "text/plain" },
    });
  };

  try {
    const response = await worker.fetch(
      makeRequest(APP_HOST, "/socket", {
        method: "GET",
        headers: accessHeaders({
          connection: "Upgrade",
          upgrade: "websocket",
          "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
          "sec-websocket-version": "13",
          "sec-websocket-protocol": "chat",
        }),
      }),
      createEnv(),
      {},
    );

    assert.equal(response.status, 200);
    assert.equal(await response.text(), "stream-ok");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].headers.connection, "Upgrade");
    assert.equal(calls[0].headers.upgrade, "websocket");
    assert.equal(calls[0].headers["sec-websocket-key"], "dGhlIHNhbXBsZSBub25jZQ==");
    assert.equal(calls[0].headers["sec-websocket-protocol"], "chat");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("preserves upgraded response metadata while sanitizing sensitive cookies", async () => {
  const originalFetch = globalThis.fetch;
  const upgradeHandle = { tag: "websocket-handle" };
  globalThis.fetch = async () => ({
    status: 101,
    statusText: "Switching Protocols",
    headers: new Headers([
      ["upgrade", "websocket"],
      ["set-cookie", "CF_Authorization=drop-me; Path=/; HttpOnly"],
      ["set-cookie", "chat_session=keep-me; Path=/; Domain=.madup.app; HttpOnly"],
    ]),
    body: null,
    webSocket: upgradeHandle,
  });

  try {
    const response = await worker.fetch(
      makeRequest(APP_HOST, "/socket", {
        method: "GET",
        headers: accessHeaders({
          connection: "Upgrade",
          upgrade: "websocket",
          "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
          "sec-websocket-version": "13",
        }),
      }),
      createEnv(),
      {},
    );

    assert.equal(response.status, 101);
    assert.equal(response.webSocket, upgradeHandle);
    assert.deepEqual(response.headers.getSetCookie(), [
      "chat_session=keep-me; Path=/; HttpOnly",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("preserves downstream WWW-Authenticate challenges from the control origin", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response("unauthorized", {
      status: 401,
      headers: {
        "www-authenticate": 'Bearer realm="mim", error="invalid_token"',
      },
    });

  try {
    const response = await worker.fetch(
      makeRequest(CONTROL_HOST, "/mcp", {
        method: "POST",
        headers: accessHeaders(),
        body: "{}",
      }),
      createEnv(),
      {},
    );

    assert.equal(response.status, 401);
    assert.equal(
      response.headers.get("www-authenticate"),
      'Bearer realm="mim", error="invalid_token"',
    );
    assert.equal(await response.text(), "unauthorized");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("preserves multiple safe cookies and drops later sensitive cookies when getSetCookie is available", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response("ok", {
      status: 200,
      headers: [
        ["set-cookie", "theme=dark; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Path=/; Domain=.madup.app"],
        ["set-cookie", "app_session=1; Path=/; Domain=.run.app; Secure; HttpOnly"],
        ["set-cookie", "CF_Authorization=drop-me; Path=/; HttpOnly"],
      ],
    });

  try {
    const response = await worker.fetch(
      makeRequest(APP_HOST, "/dashboard", {
        method: "GET",
        headers: accessHeaders(),
      }),
      createEnv(),
      {},
    );

    assert.equal(response.status, 200);
    assert.deepEqual(response.headers.getSetCookie(), [
      "theme=dark; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Path=/",
      "app_session=1; Path=/; Secure; HttpOnly",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("safely splits coalesced set-cookie values without breaking expires commas", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    const combined =
      "theme=dark; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Path=/; Domain=.madup.app, " +
      "CF_Authorization=drop-me; Path=/; HttpOnly, " +
      "app_session=1; Path=/; Domain=.run.app; Secure; HttpOnly";
    return {
      status: 200,
      statusText: "OK",
      headers: {
        entries() {
          return [
            ["content-type", "text/plain"],
            ["set-cookie", combined],
          ][Symbol.iterator]();
        },
        get(name) {
          return name.toLowerCase() === "set-cookie" ? combined : null;
        },
      },
      body: new Response("ok").body,
    };
  };

  try {
    const response = await worker.fetch(
      makeRequest(APP_HOST, "/dashboard", {
        method: "GET",
        headers: accessHeaders(),
      }),
      createEnv(),
      {},
    );

    assert.equal(response.status, 200);
    assert.deepEqual(response.headers.getSetCookie(), [
      "theme=dark; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Path=/",
      "app_session=1; Path=/; Secure; HttpOnly",
    ]);
    assert.equal(await response.text(), "ok");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fails closed when the dual-destination worker configuration is incomplete", async () => {
  let called = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    called = true;
    return new Response("unexpected");
  };

  try {
    const response = await worker.fetch(
      makeRequest(APP_HOST, "/dashboard", {
        headers: accessHeaders(),
      }),
      createEnv({
        MIM_APP_GATEWAY_ORIGIN: "",
        MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET: "",
      }),
      {},
    );

    assert.equal(response.status, 500);
    assert.equal(await response.text(), "Proxy configuration invalid.");
    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fails closed unless both origins exactly match the reviewed project services", async () => {
  let called = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    called = true;
    return new Response("unexpected");
  };

  const invalidConfigs = [
    { MIM_CONTROL_ORIGIN: "https://attacker-123456789012.asia-northeast3.run.app" },
    { MIM_CONTROL_ORIGIN: "https://mim-control-plane-999999999999.asia-northeast3.run.app" },
    { MIM_APP_GATEWAY_ORIGIN: "https://mim-app-gateway-999999999999.asia-northeast3.run.app" },
    { MIM_APP_GATEWAY_ORIGIN: "https://other-app-123456789012.asia-northeast3.run.app" },
    { MIM_CONTROL_ORIGIN: `${CONTROL_ORIGIN}.attacker.example` },
    { MIM_CONTROL_ORIGIN: `https://user@${new URL(CONTROL_ORIGIN).hostname}` },
    { MIM_CONTROL_ORIGIN: `${CONTROL_ORIGIN}:443` },
    { MIM_CONTROL_ORIGIN: `${CONTROL_ORIGIN}/healthz` },
    { MIM_APP_GATEWAY_ORIGIN: `${APP_ORIGIN}?origin=other` },
    { MIM_APP_GATEWAY_ORIGIN: `${APP_ORIGIN}#fragment` },
  ];

  try {
    for (const overrides of invalidConfigs) {
      const response = await worker.fetch(
        makeRequest(APP_HOST, "/dashboard", { headers: accessHeaders() }),
        createEnv(overrides),
        {},
      );
      assert.equal(response.status, 500);
      assert.equal(await response.text(), "Proxy configuration invalid.");
    }
    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fails closed when the reviewed project-number binding is missing or malformed", async () => {
  let called = false;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    called = true;
    return new Response("unexpected");
  };

  try {
    const invalidProjectNumbers = [
      undefined,
      "",
      "123456",
      "012345678901",
      "12345678901a",
      " 123456789012",
    ];
    for (const projectNumber of invalidProjectNumbers) {
      const env = createEnv({ MIM_PROJECT_NUMBER: projectNumber });
      if (projectNumber === undefined) {
        delete env.MIM_PROJECT_NUMBER;
      }
      const response = await worker.fetch(
        makeRequest(CONTROL_HOST, "/healthz", { headers: accessHeaders() }),
        env,
        {},
      );
      assert.equal(response.status, 500);
      assert.equal(await response.text(), "Proxy configuration invalid.");
    }
    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
