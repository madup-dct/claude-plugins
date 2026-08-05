import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_CLOUD_RUN_SERVICE = "mim-bootstrap"
HEALTH_BODY = json.dumps({"status": "ok"}, separators=(",", ":")).encode("utf-8")
INDEX_BODY = b"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Madup Infra Manager</title>
</head>
<body>
  <main>
    <h1>Madup Infra Manager</h1>
    <p>Secure infrastructure setup is in progress.</p>
  </main>
</body>
</html>
"""


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "MIM"
    sys_version = ""

    def do_GET(self):
        if self.path in {"/", "/healthz"} and not self._has_allowed_host():
            self._send(404, "text/plain; charset=utf-8", b"Not Found\n")
            return

        if self.path == "/healthz":
            self._send(200, "application/json", HEALTH_BODY)
            return

        if self.path == "/":
            self._send(200, "text/html; charset=utf-8", INDEX_BODY)
            return

        self._send(404, "text/plain; charset=utf-8", b"Not Found\n")

    def _has_allowed_host(self):
        request_host = _normalize_host(self.headers.get("Host", ""))
        if not request_host:
            return False

        configured_host = _configured_hostname()
        if not configured_host:
            return False
        if request_host == configured_host:
            return True

        return _is_allowed_cloud_run_host(request_host)

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)


def build_server(host, port):
    return ThreadingHTTPServer((host, port), RequestHandler)


def _normalize_host(host):
    value = (host or "").strip().lower()
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def _configured_hostname():
    return _normalize_host(os.environ.get("MIM_HOSTNAME", ""))


def _configured_service_name():
    value = (
        os.environ.get("K_SERVICE")
        or os.environ.get("MIM_CLOUD_RUN_SERVICE_NAME")
        or DEFAULT_CLOUD_RUN_SERVICE
    )
    return value.strip().lower()


def _is_allowed_cloud_run_host(host):
    labels = host.split(".")
    if len(labels) != 4 or labels[-2:] != ["run", "app"]:
        return False

    service_label, region_label = labels[0], labels[1]
    if not re.fullmatch(r"[a-z]+(?:-[a-z0-9]+)*[0-9]", region_label):
        return False

    if "---" in service_label:
        tag_label, _, service_label = service_label.partition("---")
        if not re.fullmatch(r"[a-z0-9-]+", tag_label):
            return False

    expected_service = re.escape(_configured_service_name())
    return re.fullmatch(rf"{expected_service}-[0-9]+", service_label) is not None


def main():
    port = int(os.environ.get("PORT", "8080"))
    build_server("0.0.0.0", port).serve_forever()


if __name__ == "__main__":
    main()
