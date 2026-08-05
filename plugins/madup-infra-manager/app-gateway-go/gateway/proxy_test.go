package gateway

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const proxyRunAppSuffix = ".run" + ".app"

func TestValidateDecisionUpstreamAcceptsOnlyReviewedRunAppShape(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	now := time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC)
	workloadID := "wrk-123"
	upstreamHost := "https://mim-svc-" + workloadSuffix(workloadID) + "-uc.a.run.app"

	if _, err := ValidateDecisionUpstream(cfg, "sample-a1b2c3d4e5f6.madup.app", AuthorizationDecision{
		PublicHost:       "sample-a1b2c3d4e5f6.madup.app",
		WorkloadID:       workloadID,
		UpstreamURL:      upstreamHost,
		UpstreamAudience: upstreamHost,
		ExpiresAt:        now.Add(30 * time.Second),
	}, now); err != nil {
		t.Fatalf("ValidateDecisionUpstream() error = %v", err)
	}

	badDecisions := []AuthorizationDecision{
		{PublicHost: "other.madup.app", WorkloadID: workloadID, UpstreamURL: upstreamHost, UpstreamAudience: upstreamHost, ExpiresAt: now.Add(30 * time.Second)},
		{PublicHost: "sample-a1b2c3d4e5f6.madup.app", WorkloadID: workloadID, UpstreamURL: "http://" + strings.TrimPrefix(upstreamHost, "https://"), UpstreamAudience: "http://" + strings.TrimPrefix(upstreamHost, "https://"), ExpiresAt: now.Add(30 * time.Second)},
		{PublicHost: "sample-a1b2c3d4e5f6.madup.app", WorkloadID: workloadID, UpstreamURL: upstreamHost + "/path", UpstreamAudience: upstreamHost + "/path", ExpiresAt: now.Add(30 * time.Second)},
		{PublicHost: "sample-a1b2c3d4e5f6.madup.app", WorkloadID: workloadID, UpstreamURL: "https://other.a" + proxyRunAppSuffix, UpstreamAudience: "https://other.a" + proxyRunAppSuffix, ExpiresAt: now.Add(30 * time.Second)},
		{PublicHost: "sample-a1b2c3d4e5f6.madup.app", WorkloadID: workloadID, UpstreamURL: upstreamHost, UpstreamAudience: "https://other.a" + proxyRunAppSuffix, ExpiresAt: now.Add(30 * time.Second)},
		{PublicHost: "sample-a1b2c3d4e5f6.madup.app", WorkloadID: "wrk-other", UpstreamURL: upstreamHost, UpstreamAudience: upstreamHost, ExpiresAt: now.Add(30 * time.Second)},
		{PublicHost: "sample-a1b2c3d4e5f6.madup.app", WorkloadID: workloadID, UpstreamURL: upstreamHost, UpstreamAudience: upstreamHost, ExpiresAt: now.Add(-1 * time.Second)},
	}
	for index, decision := range badDecisions {
		t.Run(fmt.Sprintf("bad-%d", index), func(t *testing.T) {
			if _, err := ValidateDecisionUpstream(cfg, "sample-a1b2c3d4e5f6.madup.app", decision, now); err == nil {
				t.Fatal("ValidateDecisionUpstream() succeeded unexpectedly")
			}
		})
	}
}

func TestGatewayProxyStripsCredentialsAndRewritesCookiesAndRedirects(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}

	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got := map[string]any{
			"authorization":              r.Header.Get("Authorization"),
			"proxy_authorization":        r.Header.Get("Proxy-Authorization"),
			"access_assertion":           r.Header.Get(HeaderAccessJWTAssertion),
			"origin_signature":           r.Header.Get(HeaderOriginSignature),
			"cf_connecting_ip":           r.Header.Get("CF-Connecting-IP"),
			"cdn_loop":                   r.Header.Get("CDN-Loop"),
			"true_client_ip":             r.Header.Get("True-Client-IP"),
			"x_forwarded_host":           r.Header.Get("X-Forwarded-Host"),
			"x_forwarded_proto":          r.Header.Get("X-Forwarded-Proto"),
			"x_serverless_authorization": r.Header.Get("X-Serverless-Authorization"),
			"cookie":                     r.Header.Get("Cookie"),
		}
		w.Header().Add("Set-Cookie", "session=value; Domain=.run.app; Path=/; HttpOnly")
		w.Header().Add("Set-Cookie", "local=value; Path=/; HttpOnly")
		w.Header().Set("Location", "https://mim-svc-"+workloadSuffix("wrk-123")+"-uc.a.run.app/redirected")
		w.WriteHeader(http.StatusFound)
		_ = json.NewEncoder(w).Encode(got)
	}))
	defer upstream.Close()

	localUpstreamURL, err := url.Parse(upstream.URL)
	if err != nil {
		t.Fatalf("url.Parse() error = %v", err)
	}
	upstreamURL, err := url.Parse("https://mim-svc-" + workloadSuffix("wrk-123") + "-uc.a.run.app")
	if err != nil {
		t.Fatalf("url.Parse(run.app) error = %v", err)
	}
	decision := AuthorizationDecision{
		PublicHost:       "sample-a1b2c3d4e5f6.madup.app",
		WorkloadID:       "wrk-123",
		UpstreamURL:      upstreamURL.String(),
		UpstreamAudience: upstreamURL.String(),
		ExpiresAt:        time.Date(2026, 8, 5, 10, 11, 42, 0, time.UTC),
	}
	handler := newProxyHandler(cfg, proxyHandlerDeps{
		Clock: func() time.Time { return time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC) },
		Transport: roundTripperFunc(func(r *http.Request) (*http.Response, error) {
			clone := r.Clone(r.Context())
			clone.URL.Scheme = localUpstreamURL.Scheme
			clone.URL.Host = localUpstreamURL.Host
			return upstream.Client().Transport.RoundTrip(clone)
		}),
		TokenSource: idTokenSourceFunc(func(ctx context.Context, audience string) (string, error) {
			if audience != upstreamURL.String() {
				t.Fatalf("audience = %q", audience)
			}
			return "google-id-token", nil
		}),
	})

	req := httptest.NewRequest(http.MethodGet, "https://sample-a1b2c3d4e5f6.madup.app/path", nil)
	req.Header.Set("Authorization", "Bearer browser-secret")
	req.Header.Set("Proxy-Authorization", "Basic browser-proxy-secret")
	req.Header.Set(HeaderAccessJWTAssertion, "worker-only")
	req.Header.Set(HeaderOriginSignature, "worker-proof")
	req.Header.Set("CF-Connecting-IP", "198.51.100.8")
	req.Header.Set("CDN-Loop", "cloudflare")
	req.Header.Set("True-Client-IP", "198.51.100.9")
	req.Header.Set("Cookie", "CF_Authorization=strip-me; __Host-CF_Authorization=strip-too; app_session=keep-me; __Host-mim_session=strip-too; __Secure-mim_state=strip-three")
	recorder := httptest.NewRecorder()

	handler.ServeAuthorizedHTTP(recorder, req, decision, upstreamURL, "sample-a1b2c3d4e5f6.madup.app")

	response := recorder.Result()
	defer response.Body.Close()
	if response.StatusCode != http.StatusFound {
		t.Fatalf("StatusCode = %d", response.StatusCode)
	}
	var payload map[string]string
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		t.Fatalf("json.NewDecoder() error = %v", err)
	}
	if payload["authorization"] != "" || payload["proxy_authorization"] != "" || payload["access_assertion"] != "" || payload["origin_signature"] != "" || payload["cf_connecting_ip"] != "" || payload["cdn_loop"] != "" || payload["true_client_ip"] != "" {
		t.Fatalf("sensitive headers leaked: %#v", payload)
	}
	if payload["x_forwarded_host"] != "sample-a1b2c3d4e5f6.madup.app" {
		t.Fatalf("X-Forwarded-Host = %q", payload["x_forwarded_host"])
	}
	if payload["x_forwarded_proto"] != "https" {
		t.Fatalf("X-Forwarded-Proto = %q", payload["x_forwarded_proto"])
	}
	if payload["x_serverless_authorization"] != "Bearer google-id-token" {
		t.Fatalf("X-Serverless-Authorization = %q", payload["x_serverless_authorization"])
	}
	if payload["cookie"] != "app_session=keep-me" {
		t.Fatalf("Cookie = %q", payload["cookie"])
	}
	if got := response.Header.Values("Set-Cookie"); len(got) != 2 || !strings.Contains(got[0], "Domain=sample-a1b2c3d4e5f6.madup.app") {
		t.Fatalf("Set-Cookie = %#v", got)
	}
	if got := response.Header.Get("Location"); got != "https://sample-a1b2c3d4e5f6.madup.app/redirected" {
		t.Fatalf("Location = %q", got)
	}
}

func TestGatewayProxySupportsStreamingHeadAndWebsocketUpgrade(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	handler := newProxyHandler(cfg, proxyHandlerDeps{
		Clock: func() time.Time { return time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC) },
		TokenSource: idTokenSourceFunc(func(ctx context.Context, audience string) (string, error) {
			return "google-id-token", nil
		}),
	})

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.EqualFold(r.Header.Get("Connection"), "Upgrade") && strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			hijacker, ok := w.(http.Hijacker)
			if !ok {
				t.Fatalf("response writer cannot hijack")
			}
			conn, buf, err := hijacker.Hijack()
			if err != nil {
				t.Fatalf("Hijack() error = %v", err)
			}
			defer conn.Close()
			_, _ = buf.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
			_ = buf.Flush()
			line, err := buf.ReadString('\n')
			if err != nil {
				t.Fatalf("ReadString() error = %v", err)
			}
			_, _ = buf.WriteString("echo:" + line)
			_ = buf.Flush()
			return
		}
		if r.Method == http.MethodHead {
			w.Header().Set("X-Upstream-Head", "ok")
			return
		}
		flusher, _ := w.(http.Flusher)
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("chunk-1\n"))
		if flusher != nil {
			flusher.Flush()
		}
		_, _ = w.Write([]byte("chunk-2\n"))
	}))
	defer upstream.Close()

	upstreamURL, err := url.Parse(upstream.URL)
	if err != nil {
		t.Fatalf("url.Parse() error = %v", err)
	}
	handler.deps.Transport = upstream.Client().Transport

	streamReq := httptest.NewRequest(http.MethodGet, "https://sample-a1b2c3d4e5f6.madup.app/stream", nil)
	streamRec := httptest.NewRecorder()
	handler.ServeAuthorizedHTTP(streamRec, streamReq, AuthorizationDecision{
		PublicHost:       "sample-a1b2c3d4e5f6.madup.app",
		WorkloadID:       "wrk-123",
		UpstreamURL:      upstream.URL,
		UpstreamAudience: upstream.URL,
		ExpiresAt:        time.Date(2026, 8, 5, 10, 11, 42, 0, time.UTC),
	}, upstreamURL, "sample-a1b2c3d4e5f6.madup.app")
	if got := streamRec.Body.String(); got != "chunk-1\nchunk-2\n" {
		t.Fatalf("stream body = %q", got)
	}

	headReq := httptest.NewRequest(http.MethodHead, "https://sample-a1b2c3d4e5f6.madup.app/head", nil)
	headRec := httptest.NewRecorder()
	handler.ServeAuthorizedHTTP(headRec, headReq, AuthorizationDecision{
		PublicHost:       "sample-a1b2c3d4e5f6.madup.app",
		WorkloadID:       "wrk-123",
		UpstreamURL:      upstream.URL,
		UpstreamAudience: upstream.URL,
		ExpiresAt:        time.Date(2026, 8, 5, 10, 11, 42, 0, time.UTC),
	}, upstreamURL, "sample-a1b2c3d4e5f6.madup.app")
	if got := headRec.Header().Get("X-Upstream-Head"); got != "ok" {
		t.Fatalf("X-Upstream-Head = %q", got)
	}

	gateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		handler.ServeAuthorizedHTTP(w, r, AuthorizationDecision{
			PublicHost:       "sample-a1b2c3d4e5f6.madup.app",
			WorkloadID:       "wrk-123",
			UpstreamURL:      upstream.URL,
			UpstreamAudience: upstream.URL,
			ExpiresAt:        time.Date(2026, 8, 5, 10, 11, 42, 0, time.UTC),
		}, upstreamURL, "sample-a1b2c3d4e5f6.madup.app")
	}))
	defer gateway.Close()

	gatewayURL, err := url.Parse(gateway.URL)
	if err != nil {
		t.Fatalf("url.Parse(gateway) error = %v", err)
	}
	conn, err := net.Dial("tcp", gatewayURL.Host)
	if err != nil {
		t.Fatalf("net.Dial() error = %v", err)
	}
	defer conn.Close()
	if _, err := io.WriteString(conn, "GET /ws HTTP/1.1\r\nHost: sample-a1b2c3d4e5f6.madup.app\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n"); err != nil {
		t.Fatalf("WriteString() error = %v", err)
	}
	reader := bufio.NewReader(conn)
	status, err := reader.ReadString('\n')
	if err != nil {
		t.Fatalf("ReadString(status) error = %v", err)
	}
	if !strings.Contains(status, "101") {
		t.Fatalf("status line = %q", status)
	}
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("ReadString(headers) error = %v", err)
		}
		if line == "\r\n" {
			break
		}
	}
	if _, err := io.WriteString(conn, "ping\n"); err != nil {
		t.Fatalf("WriteString(payload) error = %v", err)
	}
	echo, err := reader.ReadString('\n')
	if err != nil {
		t.Fatalf("ReadString(echo) error = %v", err)
	}
	if echo != "echo:ping\n" {
		t.Fatalf("echo = %q", echo)
	}
}

func TestGatewayProxyReturnsGenericErrorsWithoutLeaks(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	var tokenCalls atomic.Int32
	handler := newProxyHandler(cfg, proxyHandlerDeps{
		Clock: func() time.Time { return time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC) },
		TokenSource: idTokenSourceFunc(func(ctx context.Context, audience string) (string, error) {
			tokenCalls.Add(1)
			return "google-id-token", nil
		}),
		Transport: roundTripperFunc(func(r *http.Request) (*http.Response, error) {
			return nil, io.ErrUnexpectedEOF
		}),
	})
	upstreamURL, _ := url.Parse("https://mim-svc-a1b2c3d4e5f6-uc.a" + proxyRunAppSuffix)
	req := httptest.NewRequest(http.MethodGet, "https://sample-a1b2c3d4e5f6.madup.app/path", nil)
	recorder := httptest.NewRecorder()

	handler.ServeAuthorizedHTTP(recorder, req, AuthorizationDecision{
		PublicHost:       "sample-a1b2c3d4e5f6.madup.app",
		WorkloadID:       "wrk-123",
		UpstreamURL:      upstreamURL.String(),
		UpstreamAudience: upstreamURL.String(),
		ExpiresAt:        time.Date(2026, 8, 5, 10, 11, 42, 0, time.UTC),
	}, upstreamURL, "sample-a1b2c3d4e5f6.madup.app")

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("StatusCode = %d", recorder.Code)
	}
	if body := recorder.Body.String(); strings.Contains(body, "mim-svc") || strings.Contains(body, "google-id-token") {
		t.Fatalf("error leaked sensitive material: %q", body)
	}
	if tokenCalls.Load() != 1 {
		t.Fatalf("token source calls = %d", tokenCalls.Load())
	}
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (fn roundTripperFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return fn(req)
}
