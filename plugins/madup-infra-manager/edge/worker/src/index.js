const encoder = new TextEncoder();

export const ORIGIN_HEADER_KEY_ID = "X-MIM-Origin-Key-Id";
export const ORIGIN_HEADER_TIMESTAMP = "X-MIM-Origin-Timestamp";
export const ORIGIN_HEADER_REQUEST_ID = "X-MIM-Origin-Request-Id";
export const ORIGIN_HEADER_PUBLIC_HOST = "X-MIM-Origin-Public-Host";
export const ORIGIN_HEADER_DESTINATION_CLASS = "X-MIM-Origin-Destination-Class";
export const ORIGIN_HEADER_SIGNATURE = "X-MIM-Origin-Signature";
export const ACCESS_ASSERTION_HEADER = "Cf-Access-Jwt-Assertion";
export const GITHUB_SIGNATURE_HEADER = "X-Hub-Signature-256";
export const GITHUB_EVENT_HEADER = "X-GitHub-Event";
export const GITHUB_DELIVERY_HEADER = "X-GitHub-Delivery";

const DENIED_TEXT = "Request denied.";
const INVALID_CONFIG_TEXT = "Proxy configuration invalid.";
const ORIGIN_ERROR_TEXT = "Origin unavailable.";
const PAYLOAD_TOO_LARGE_TEXT = "Payload too large.";
const REQUEST_ERROR_TEXT = "Request could not be processed.";
const CONTROL_MAX_BODY_BYTES = 16 * 1024;
const APP_MAX_BODY_BYTES = 1024 * 1024;
const FIXED_REGION = "asia-northeast3";
const CONTROL_SERVICE_NAME = "mim-control-plane";
const APP_GATEWAY_SERVICE_NAME = "mim-app-gateway";
const KEY_ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/;
const PROJECT_NUMBER_PATTERN = /^[1-9][0-9]{11}$/;
const METHOD_PATTERN = /^[A-Z]{3,16}$/;
const REQUEST_TARGET_PATTERN = /^\/[A-Za-z0-9._~!$&'()*+,;=:@/?-]*$/;
const APP_REQUEST_TARGET_PATTERN = /^\/[A-Za-z0-9._~!$&'()*+,;=:@/?%-]*$/;
const TEMPLATE_SEGMENT_PATTERN = /^[A-Za-z0-9._-]{1,128}$/;
const TEMPLATE_TOKEN_PATTERN = /^\{[a-z][a-z0-9_]{0,31}\}$/;
const HOST_LABEL_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const COOKIE_PAIR_START_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+=./;
const APP_ALLOWED_METHODS = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]);
const RESERVED_APP_HOSTS = new Set(["www"]);
const GITHUB_WEBHOOK_ROUTE = "POST /v1/webhooks/github";

export default {
  async fetch(request, env) {
    const config = parseConfig(env);
    if (config === null) {
      return textResponse(INVALID_CONFIG_TEXT, 500);
    }

    const url = safeUrl(request.url);
    if (
      url === null ||
      url.protocol !== "https:" ||
      url.port !== "" ||
      url.username ||
      url.password
    ) {
      return textResponse(DENIED_TEXT, 403);
    }

    const destination = classifyDestination(config, url.hostname);
    if (destination === null) {
      return textResponse(DENIED_TEXT, 403);
    }

    const route = selectRoute(config, destination, normalizeMethod(request.method), url);
    if (route === null) {
      return textResponse(
        hasPathMatch(config.controlRoutes, url.pathname) ? "Method not allowed." : "Route not available.",
        hasPathMatch(config.controlRoutes, url.pathname) ? 405 : 404,
      );
    }
    if (route.errorStatus !== undefined) {
      return textResponse(route.errorStatus === 405 ? "Method not allowed." : "Route not available.", route.errorStatus);
    }

    const gitHubForwardHeaders =
      route.authMode === "github_webhook" ? readGitHubForwardHeaders(request.headers) : null;
    const accessAssertion = requireAccessAssertion(request.headers, route.authMode);
    if (route.authMode === "access_jwt" && accessAssertion === null) {
      return textResponse(DENIED_TEXT, 403);
    }
    if (route.authMode === "github_webhook" && gitHubForwardHeaders === null) {
      return textResponse(DENIED_TEXT, 403);
    }

    const preparedBody = await prepareBody(request, route.maxBodyBytes);
    if (preparedBody.kind === "too_large") {
      return textResponse(PAYLOAD_TOO_LARGE_TEXT, 413);
    }
    if (preparedBody.kind === "error") {
      return textResponse(REQUEST_ERROR_TEXT, 400);
    }

    const requestId = crypto.randomUUID();
    const timestamp = Math.floor(Date.now() / 1000);
    const signature = await signRequest({
      secret: route.secret,
      destinationClass: route.destinationClass,
      method: route.method,
      publicHost: destination.publicHost,
      path: canonicalPath(url),
      bodyBytes: preparedBody.bodyBytes,
      timestamp,
      requestId,
      keyId: route.keyId,
    });
    if (signature === null) {
      return textResponse(INVALID_CONFIG_TEXT, 500);
    }

    const forwardHeaders = buildForwardHeaders(request.headers, route.authMode, gitHubForwardHeaders);
    if (accessAssertion !== null) {
      forwardHeaders.set(ACCESS_ASSERTION_HEADER, accessAssertion);
    }
    forwardHeaders.set(ORIGIN_HEADER_KEY_ID, route.keyId);
    forwardHeaders.set(ORIGIN_HEADER_TIMESTAMP, String(timestamp));
    forwardHeaders.set(ORIGIN_HEADER_REQUEST_ID, requestId);
    forwardHeaders.set(ORIGIN_HEADER_PUBLIC_HOST, destination.publicHost);
    forwardHeaders.set(ORIGIN_HEADER_DESTINATION_CLASS, route.destinationClass);
    forwardHeaders.set(ORIGIN_HEADER_SIGNATURE, signature);

    const originUrl = new URL(route.origin);
    originUrl.pathname = url.pathname;
    originUrl.search = url.search;

    try {
      const init = {
        method: route.method,
        headers: forwardHeaders,
        body: preparedBody.forwardBody,
        redirect: "manual",
      };
      if (preparedBody.forwardBody instanceof ReadableStream) {
        init.duplex = "half";
      }
      const originResponse = await fetch(
        new Request(originUrl.toString(), init),
      );
      return sanitizeOriginResponse(originResponse);
    } catch {
      return textResponse(ORIGIN_ERROR_TEXT, 502);
    }
  },
};

function parseConfig(env) {
  if (typeof env !== "object" || env === null) {
    return null;
  }

  const controlHostname = readHostname(env.MIM_CONTROL_PUBLIC_HOSTNAME);
  const appHostSuffix = readHostname(env.MIM_APP_HOST_SUFFIX);
  const projectNumber = readProjectNumber(env.MIM_PROJECT_NUMBER);
  const controlOrigin = readExactOrigin(
    env.MIM_CONTROL_ORIGIN,
    projectNumber,
    CONTROL_SERVICE_NAME,
  );
  const appOrigin = readExactOrigin(
    env.MIM_APP_GATEWAY_ORIGIN,
    projectNumber,
    APP_GATEWAY_SERVICE_NAME,
  );
  const controlKeyId = readKeyId(env.MIM_CONTROL_ORIGIN_HMAC_KEY_ID);
  const appKeyId = readKeyId(env.MIM_APP_GATEWAY_ORIGIN_HMAC_KEY_ID);
  const controlSecret = readSecret(env.MIM_CONTROL_ORIGIN_HMAC_SECRET);
  const appSecret = readSecret(env.MIM_APP_GATEWAY_ORIGIN_HMAC_SECRET);
  const controlRoutes = parseAllowedRoutes(env.MIM_CONTROL_ALLOWED_ROUTES);

  if (
    controlHostname === null ||
    appHostSuffix === null ||
    controlOrigin === null ||
    appOrigin === null ||
    controlKeyId === null ||
    appKeyId === null ||
    controlSecret === null ||
    appSecret === null ||
    controlRoutes === null
  ) {
    return null;
  }

  if (!controlHostname.endsWith(`.${appHostSuffix}`)) {
    return null;
  }
  const controlLabel = controlHostname.slice(0, -(appHostSuffix.length + 1));
  if (!isValidAppSlug(controlLabel, new Set())) {
    return null;
  }

  const reservedAppHosts = new Set(RESERVED_APP_HOSTS);
  reservedAppHosts.add(controlLabel);

  return {
    controlHostname,
    appHostSuffix,
    controlRoutes,
    reservedAppHosts,
    control: {
      origin: controlOrigin,
      keyId: controlKeyId,
      secret: controlSecret,
      destinationClass: "control-plane",
    },
    app: {
      origin: appOrigin,
      keyId: appKeyId,
      secret: appSecret,
      destinationClass: "app-gateway",
    },
  };
}

function readHostname(value) {
  if (typeof value !== "string") {
    return null;
  }
  const hostname = value.trim().toLowerCase();
  if (!hostname || hostname.startsWith(".") || hostname.endsWith(".")) {
    return null;
  }
  const labels = hostname.split(".");
  if (labels.length < 2 || labels.some((label) => !HOST_LABEL_PATTERN.test(label))) {
    return null;
  }
  return hostname;
}

function readProjectNumber(value) {
  return typeof value === "string" && PROJECT_NUMBER_PATTERN.test(value) ? value : null;
}

function readExactOrigin(value, projectNumber, serviceName) {
  if (typeof value !== "string" || projectNumber === null) {
    return null;
  }
  const expected = `https://${serviceName}-${projectNumber}.${FIXED_REGION}.run.app`;
  return value === expected ? expected : null;
}

function readKeyId(value) {
  if (typeof value !== "string") {
    return null;
  }
  const keyId = value.trim();
  return KEY_ID_PATTERN.test(keyId) ? keyId : null;
}

function readSecret(value) {
  if (typeof value !== "string") {
    return null;
  }
  return encoder.encode(value).byteLength >= 32 ? value : null;
}

function classifyDestination(config, hostname) {
  const publicHost = hostname.toLowerCase();
  if (publicHost === config.controlHostname) {
    return {
      publicHost,
      destinationClass: config.control.destinationClass,
      origin: config.control.origin,
      keyId: config.control.keyId,
      secret: config.control.secret,
    };
  }
  if (publicHost === config.appHostSuffix || !publicHost.endsWith(`.${config.appHostSuffix}`)) {
    return null;
  }
  const slug = publicHost.slice(0, -(config.appHostSuffix.length + 1));
  if (!isValidAppSlug(slug, config.reservedAppHosts)) {
    return null;
  }
  return {
    publicHost,
    destinationClass: config.app.destinationClass,
    origin: config.app.origin,
    keyId: config.app.keyId,
    secret: config.app.secret,
  };
}

function isValidAppSlug(value, reservedHosts) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    !value.includes(".") &&
    !value.startsWith("xn--") &&
    HOST_LABEL_PATTERN.test(value) &&
    !reservedHosts.has(value)
  );
}

function normalizeMethod(value) {
  return typeof value === "string" ? value.trim().toUpperCase() : "";
}

function selectRoute(config, destination, method, url) {
  if (METHOD_PATTERN.test(method) === false) {
    return { errorStatus: 405 };
  }
  if (!isAllowedRequestTarget(url, destination.destinationClass)) {
    return { errorStatus: 404 };
  }
  if (destination.destinationClass === "control-plane") {
    const matchedRoute = matchAllowedRoute(config.controlRoutes, method, url);
    if (matchedRoute === null) {
      return { errorStatus: hasPathMatch(config.controlRoutes, url.pathname) ? 405 : 404 };
    }
    return {
      method,
      authMode: matchedRoute.authMode,
      destinationClass: destination.destinationClass,
      origin: destination.origin,
      keyId: destination.keyId,
      secret: destination.secret,
      maxBodyBytes: CONTROL_MAX_BODY_BYTES,
    };
  }
  if (!APP_ALLOWED_METHODS.has(method)) {
    return { errorStatus: 405 };
  }
  return {
    method,
    authMode: "access_jwt",
    destinationClass: destination.destinationClass,
    origin: destination.origin,
    keyId: destination.keyId,
    secret: destination.secret,
    maxBodyBytes: APP_MAX_BODY_BYTES,
  };
}

function parseAllowedRoutes(value) {
  if (typeof value !== "string") {
    return null;
  }
  const allowedRoutes = [];
  for (const rawLine of value.split("\n")) {
    const line = rawLine.trim();
    if (!line) {
      continue;
    }
    const spaceIndex = line.indexOf(" ");
    if (spaceIndex <= 0) {
      return null;
    }
    const method = line.slice(0, spaceIndex).trim().toUpperCase();
    const path = line.slice(spaceIndex + 1).trim();
    if (!METHOD_PATTERN.test(method)) {
      return null;
    }
    const route = compileAllowedRoute(method, path);
    if (route === null) {
      return null;
    }
    allowedRoutes.push(route);
  }
  return allowedRoutes.length > 0 ? allowedRoutes : null;
}

function compileAllowedRoute(method, path) {
  if (path.endsWith("?*")) {
    return compileQueryWildcardRoute(method, path);
  }
  if (path.includes("{") || path.includes("}")) {
    return compileTemplateRoute(method, path);
  }
  if (!isCanonicalPath(path)) {
    return null;
  }
  return {
    method,
    type: "exact",
    path,
    pathname: splitPathAndQuery(path).pathname,
    authMode: method === "POST" && path === "/v1/webhooks/github" ? "github_webhook" : "access_jwt",
  };
}

function compileQueryWildcardRoute(method, path) {
  const pathname = path.slice(0, -2);
  if (!pathname || !isCanonicalPath(pathname) || pathname.includes("?")) {
    return null;
  }
  return {
    method,
    type: "query_wildcard",
    path,
    pathname,
    authMode:
      method === "POST" && pathname === "/v1/webhooks/github"
        ? "github_webhook"
        : "access_jwt",
  };
}

function compileTemplateRoute(method, path) {
  if (!path.startsWith("/") || path.includes("?") || path.includes("#") || path.includes("\\") || path.includes("%")) {
    return null;
  }
  const segments = path.split("/").slice(1);
  if (segments.length === 0) {
    return null;
  }
  const regexParts = [];
  for (const segment of segments) {
    if (!segment) {
      return null;
    }
    if (TEMPLATE_TOKEN_PATTERN.test(segment)) {
      regexParts.push("([A-Za-z0-9._-]{1,128})");
      continue;
    }
    if (!TEMPLATE_SEGMENT_PATTERN.test(segment)) {
      return null;
    }
    regexParts.push(escapeRegex(segment));
  }
  return {
    method,
    type: "template",
    path,
    pathname: path,
    pattern: new RegExp(`^/${regexParts.join("/")}$`),
    authMode: "access_jwt",
  };
}

function safeUrl(value) {
  try {
    return new URL(value);
  } catch {
    return null;
  }
}

function canonicalPath(url) {
  return `${url.pathname}${url.search}`;
}

function isCanonicalPath(value) {
  return isControlPlaneRequestTarget(value);
}

function isAllowedRequestTarget(url, destinationClass) {
  if (!(url instanceof URL)) {
    return false;
  }
  const target = canonicalPath(url);
  if (destinationClass === "app-gateway") {
    return isAppGatewayRequestTarget(target, url.hash);
  }
  return isControlPlaneRequestTarget(target, url.hash);
}

function isControlPlaneRequestTarget(value, hash = "") {
  if (hash) {
    return false;
  }
  if (!value.startsWith("/") || value.startsWith("//") || value.includes("#") || value.includes("\\")) {
    return false;
  }
  if (value.includes("http://") || value.includes("https://") || value.includes("%")) {
    return false;
  }
  if (value.split("?").length > 2) {
    return false;
  }
  return REQUEST_TARGET_PATTERN.test(value);
}

function isAppGatewayRequestTarget(value, hash = "") {
  if (hash) {
    return false;
  }
  if (!value.startsWith("/") || value.startsWith("//") || value.includes("#") || value.includes("\\")) {
    return false;
  }
  if (value.includes("http://") || value.includes("https://")) {
    return false;
  }
  if (value.split("?").length > 2) {
    return false;
  }
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code < 0x21 || code > 0x7e) {
      return false;
    }
    if (value[index] !== "%") {
      continue;
    }
    const first = value[index + 1];
    const second = value[index + 2];
    if (!isHexDigit(first) || !isHexDigit(second)) {
      return false;
    }
    index += 2;
  }
  return APP_REQUEST_TARGET_PATTERN.test(value);
}

function isHexDigit(value) {
  return typeof value === "string" && /^[0-9A-Fa-f]$/.test(value);
}

function splitPathAndQuery(value) {
  const questionIndex = value.indexOf("?");
  if (questionIndex === -1) {
    return { pathname: value, search: "" };
  }
  return {
    pathname: value.slice(0, questionIndex),
    search: value.slice(questionIndex),
  };
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function matchAllowedRoute(allowedRoutes, method, url) {
  const target = canonicalPath(url);
  for (const route of allowedRoutes) {
    if (route.method !== method) {
      continue;
    }
    if (route.type === "exact" && route.path === target) {
      return route;
    }
    if (route.type === "query_wildcard" && route.pathname === url.pathname) {
      return route;
    }
    if (route.type === "template" && !url.search && route.pattern.test(url.pathname)) {
      return route;
    }
  }
  return null;
}

function hasPathMatch(allowedRoutes, pathname) {
  for (const route of allowedRoutes) {
    if ((route.type === "exact" || route.type === "query_wildcard") && route.pathname === pathname) {
      return true;
    }
    if (route.type === "template" && route.pattern.test(pathname)) {
      return true;
    }
  }
  return false;
}

function requireAccessAssertion(headers, authMode) {
  if (authMode !== "access_jwt") {
    return null;
  }
  const assertion = headers.get(ACCESS_ASSERTION_HEADER);
  if (typeof assertion !== "string") {
    return null;
  }
  const trimmed = assertion.trim();
  if (!trimmed || trimmed.includes(",")) {
    return null;
  }
  return trimmed;
}

function readGitHubForwardHeaders(headers) {
  const signature = headers.get(GITHUB_SIGNATURE_HEADER);
  const event = headers.get(GITHUB_EVENT_HEADER);
  const delivery = headers.get(GITHUB_DELIVERY_HEADER);
  if (
    typeof signature !== "string" ||
    typeof event !== "string" ||
    typeof delivery !== "string" ||
    !/^sha256=[0-9a-f]{64}$/i.test(signature.trim()) ||
    !/^[A-Za-z0-9._-]{1,64}$/.test(event.trim()) ||
    !/^[A-Za-z0-9-]{1,128}$/.test(delivery.trim())
  ) {
    return null;
  }
  return {
    signature: signature.trim(),
    event: event.trim(),
    delivery: delivery.trim(),
  };
}

function buildForwardHeaders(headers, authMode, gitHubForwardHeaders) {
  if (authMode === "github_webhook") {
    const forwarded = new Headers();
    forwarded.set(GITHUB_SIGNATURE_HEADER, gitHubForwardHeaders.signature);
    forwarded.set(GITHUB_EVENT_HEADER, gitHubForwardHeaders.event);
    forwarded.set(GITHUB_DELIVERY_HEADER, gitHubForwardHeaders.delivery);
    return forwarded;
  }
  const forwarded = new Headers();
  for (const [name, value] of headers.entries()) {
    const lowerName = name.toLowerCase();
    if (
      lowerName === ACCESS_ASSERTION_HEADER.toLowerCase() ||
      lowerName.startsWith("cf-") ||
      lowerName === "authorization" ||
      lowerName === "proxy-authorization" ||
      lowerName === "true-client-ip" ||
      lowerName === "x-real-ip" ||
      lowerName === "cdn-loop" ||
      lowerName === "forwarded" ||
      lowerName.startsWith("x-forwarded-") ||
      lowerName.startsWith("x-mim-") ||
      lowerName.startsWith("cf-access-")
    ) {
      continue;
    }
    if (lowerName === "cookie") {
      const filteredCookie = filterCookieHeader(value);
      if (filteredCookie !== null) {
        forwarded.set(name, filteredCookie);
      }
      continue;
    }
    forwarded.set(name, value);
  }
  return forwarded;
}

function filterCookieHeader(value) {
  if (typeof value !== "string") {
    return null;
  }
  const kept = [];
  for (const cookie of value.split(";")) {
    const trimmed = cookie.trim();
    if (!trimmed) {
      continue;
    }
    const separatorIndex = trimmed.indexOf("=");
    const name = (separatorIndex === -1 ? trimmed : trimmed.slice(0, separatorIndex)).trim();
    if (!name || isSensitiveCookieName(name)) {
      continue;
    }
    kept.push(trimmed);
  }
  return kept.length > 0 ? kept.join("; ") : null;
}

function isSensitiveCookieName(name) {
  const lowerName = name.toLowerCase();
  return (
    lowerName === "cf_authorization" ||
    lowerName === "__secure-cf_authorization" ||
    lowerName === "__host-cf_authorization" ||
    lowerName.startsWith("mim_") ||
    lowerName.startsWith("mim-") ||
    lowerName.startsWith("__host-mim") ||
    lowerName.startsWith("__secure-mim")
  );
}

async function prepareBody(request, maxBodyBytes) {
  if (request.method === "GET" || request.method === "HEAD" || request.body == null) {
    return {
      kind: "ok",
      bodyBytes: new Uint8Array(),
      forwardBody: null,
    };
  }

  const declaredLength = parseContentLength(request.headers.get("content-length"));
  if (declaredLength !== null && declaredLength > maxBodyBytes) {
    return { kind: "too_large" };
  }

  if (!(request.body instanceof ReadableStream)) {
    try {
      const bodyBytes = await new Response(request.body).arrayBuffer();
      if (bodyBytes.byteLength > maxBodyBytes) {
        return { kind: "too_large" };
      }
      return {
        kind: "ok",
        bodyBytes: new Uint8Array(bodyBytes),
        forwardBody: request.body,
      };
    } catch {
      return { kind: "error" };
    }
  }

  const [hashBody, forwardBody] = request.body.tee();
  const bodyResult = await readBodyWithinLimit(hashBody, maxBodyBytes);
  if (bodyResult.kind === "too_large") {
    cancelStream(forwardBody);
    return bodyResult;
  }
  if (bodyResult.kind === "error") {
    cancelStream(forwardBody);
    return bodyResult;
  }
  return {
    kind: "ok",
    bodyBytes: bodyResult.bodyBytes,
    forwardBody,
  };
}

function parseContentLength(value) {
  if (typeof value !== "string" || !/^\d+$/.test(value)) {
    return null;
  }
  return Number(value);
}

async function readBodyWithinLimit(stream, maxBytes) {
  const reader = stream.getReader();
  const chunks = [];
  let totalBytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
      totalBytes += chunk.byteLength;
      if (totalBytes > maxBytes) {
        return { kind: "too_large" };
      }
      chunks.push(chunk);
    }
  } catch {
    return { kind: "error" };
  } finally {
    reader.releaseLock();
  }

  const bodyBytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bodyBytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return {
    kind: "ok",
    bodyBytes,
  };
}

function cancelStream(stream) {
  if (!(stream instanceof ReadableStream)) {
    return;
  }
  void stream.cancel().catch(() => {
    // Best-effort cleanup only.
  });
}

async function signRequest({
  secret,
  destinationClass,
  method,
  publicHost,
  path,
  bodyBytes,
  timestamp,
  requestId,
  keyId,
}) {
  try {
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
      await sha256Hex(bodyBytes),
      String(timestamp),
      requestId,
      keyId,
    ].join("\n");
    const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
    return hex(signature);
  } catch {
    return null;
  }
}

async function sha256Hex(bytes) {
  return hex(await crypto.subtle.digest("SHA-256", bytes));
}

function hex(value) {
  return Array.from(new Uint8Array(value), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function sanitizeOriginResponse(response) {
  const headers = sanitizeResponseHeaders(response.headers);
  if (response.webSocket !== undefined) {
    try {
      return new Response(null, {
        status: response.status,
        statusText: response.statusText,
        headers,
        webSocket: response.webSocket,
      });
    } catch {
      return {
        status: response.status,
        statusText: response.statusText,
        headers,
        body: response.body,
        webSocket: response.webSocket,
      };
    }
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function sanitizeResponseHeaders(sourceHeaders) {
  const headers = new Headers();
  for (const [name, value] of sourceHeaders.entries()) {
    if (name.toLowerCase() === "set-cookie") {
      continue;
    }
    headers.append(name, value);
  }
  for (const value of getSetCookieValues(sourceHeaders)) {
    const sanitized = sanitizeSetCookie(value);
    if (sanitized !== null) {
      headers.append("set-cookie", sanitized);
    }
  }
  return headers;
}

function getSetCookieValues(sourceHeaders) {
  if (typeof sourceHeaders?.getSetCookie === "function") {
    const values = sourceHeaders.getSetCookie();
    if (Array.isArray(values) && values.length > 0) {
      return values;
    }
  }

  const values = [];
  if (typeof sourceHeaders?.entries === "function") {
    for (const [name, value] of sourceHeaders.entries()) {
      if (name.toLowerCase() === "set-cookie") {
        values.push(...splitCoalescedSetCookieHeader(value));
      }
    }
  }
  if (values.length > 0) {
    return values;
  }

  if (typeof sourceHeaders?.get === "function") {
    return splitCoalescedSetCookieHeader(sourceHeaders.get("set-cookie"));
  }
  return [];
}

function splitCoalescedSetCookieHeader(value) {
  if (typeof value !== "string" || !value.trim()) {
    return [];
  }
  const parts = [];
  let start = 0;
  let inExpires = false;
  for (let index = 0; index < value.length; index += 1) {
    if (!inExpires && value.slice(index, index + 8).toLowerCase() === "expires=") {
      inExpires = true;
      index += 7;
      continue;
    }
    const char = value[index];
    if (inExpires) {
      if (char === ";") {
        inExpires = false;
      }
      continue;
    }
    if (char !== ",") {
      continue;
    }
    const remainder = value.slice(index + 1).trimStart();
    if (!COOKIE_PAIR_START_PATTERN.test(remainder)) {
      continue;
    }
    const candidate = value.slice(start, index).trim();
    if (candidate) {
      parts.push(candidate);
    }
    start = index + 1;
  }
  const last = value.slice(start).trim();
  if (last) {
    parts.push(last);
  }
  return parts;
}

function sanitizeSetCookie(value) {
  if (typeof value !== "string") {
    return null;
  }
  const firstSegment = value.split(";", 1)[0]?.trim() ?? "";
  if (!firstSegment) {
    return null;
  }
  const separatorIndex = firstSegment.indexOf("=");
  const name = (separatorIndex === -1 ? firstSegment : firstSegment.slice(0, separatorIndex)).trim();
  if (!name || isSensitiveCookieName(name)) {
    return null;
  }
  return stripCookieDomain(value);
}

function stripCookieDomain(value) {
  if (typeof value !== "string") {
    return value;
  }
  const parts = value.split(";").map((part) => part.trim()).filter(Boolean);
  if (parts.length === 0) {
    return value;
  }
  const filtered = [parts[0], ...parts.slice(1).filter((part) => !/^domain=/i.test(part))];
  return filtered.join("; ");
}

function textResponse(body, status) {
  return new Response(body, {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}
