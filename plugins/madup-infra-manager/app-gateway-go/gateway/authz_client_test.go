package gateway

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

const authzRunAppSuffix = ".run" + ".app"

func TestAuthorizationClientSendsExactPrivateAuthRequest(t *testing.T) {
	now := time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC)
	var sourceCalls atomic.Int32
	source := idTokenSourceFunc(func(ctx context.Context, audience string) (string, error) {
		sourceCalls.Add(1)
		if audience != "https://mim-schedule-gateway-123456789012.asia-northeast3"+authzRunAppSuffix {
			t.Fatalf("audience = %q", audience)
		}
		return "schedule-gateway-token", nil
	})

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("method = %s", r.Method)
		}
		if r.URL.Path != "/v1/apps/authorize" {
			t.Fatalf("path = %q", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer schedule-gateway-token" {
			t.Fatalf("Authorization = %q", got)
		}
		if got := r.Header.Get("Content-Type"); got != "application/json" {
			t.Fatalf("Content-Type = %q", got)
		}
		body, _ := io.ReadAll(r.Body)
		defer r.Body.Close()
		var payload map[string]any
		if err := json.Unmarshal(body, &payload); err != nil {
			t.Fatalf("json.Unmarshal() error = %v", err)
		}
		want := map[string]any{
			"schema":           "mim.app-authorization.v1",
			"public_host":      "sample-a1b2c3d4e5f6.madup.app",
			"method":           "GET",
			"request_target":   "/path?query=value",
			"access_subject":   "user-123",
			"access_email":     "person@madup.com",
			"edge_request_id":  "11111111-2222-4333-8444-555555555555",
			"edge_timestamp":   float64(now.Unix()),
			"edge_body_sha256": "ab",
		}
		for key, value := range want {
			if payload[key] != value {
				t.Fatalf("%s = %#v, want %#v", key, payload[key], value)
			}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"schema":            "mim.app-authorization.v1",
			"public_host":       "sample-a1b2c3d4e5f6.madup.app",
			"workload_id":       "wrk-123",
			"upstream_url":      "https://mim-svc-a1b2c3d4e5f6-uc.a" + authzRunAppSuffix,
			"upstream_audience": "https://mim-svc-a1b2c3d4e5f6-uc.a" + authzRunAppSuffix,
			"expires_at":        "2026-08-05T10:11:42Z",
		})
	}))
	defer server.Close()

	client, err := NewAuthorizationClient(AuthorizationClientConfig{
		URL:         server.URL + "/v1/apps/authorize",
		Audience:    "https://mim-schedule-gateway-123456789012.asia-northeast3" + authzRunAppSuffix,
		TokenSource: source,
		Client:      server.Client(),
	})
	if err != nil {
		t.Fatalf("NewAuthorizationClient() error = %v", err)
	}

	decision, err := client.Authorize(context.Background(), AuthorizationRequest{
		PublicHost:     "sample-a1b2c3d4e5f6.madup.app",
		Method:         http.MethodGet,
		RequestTarget:  "/path?query=value",
		AccessSubject:  "user-123",
		AccessEmail:    "person@madup.com",
		EdgeRequestID:  "11111111-2222-4333-8444-555555555555",
		EdgeTimestamp:  now,
		EdgeBodySHA256: "ab",
	})
	if err != nil {
		t.Fatalf("Authorize() error = %v", err)
	}
	if decision.WorkloadID != "wrk-123" {
		t.Fatalf("WorkloadID = %q", decision.WorkloadID)
	}
	if sourceCalls.Load() != 1 {
		t.Fatalf("Token source calls = %d", sourceCalls.Load())
	}
}

func TestAuthorizationClientDoesNotRetryAmbiguousOrDeniedRequests(t *testing.T) {
	cases := []int{http.StatusNotFound, http.StatusGatewayTimeout, http.StatusInternalServerError}
	for _, status := range cases {
		t.Run(http.StatusText(status), func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				http.Error(w, "nope", status)
			}))
			defer server.Close()

			client, err := NewAuthorizationClient(AuthorizationClientConfig{
				URL:      server.URL,
				Audience: "https://mim-schedule-gateway-123456789012.asia-northeast3" + authzRunAppSuffix,
				TokenSource: idTokenSourceFunc(func(ctx context.Context, audience string) (string, error) {
					return "token", nil
				}),
				Client: server.Client(),
			})
			if err != nil {
				t.Fatalf("NewAuthorizationClient() error = %v", err)
			}
			if _, err := client.Authorize(context.Background(), AuthorizationRequest{
				PublicHost:     "sample-a1b2c3d4e5f6.madup.app",
				Method:         http.MethodGet,
				RequestTarget:  "/",
				AccessSubject:  "user-123",
				AccessEmail:    "person@madup.com",
				EdgeRequestID:  "11111111-2222-4333-8444-555555555555",
				EdgeTimestamp:  time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC),
				EdgeBodySHA256: "ab",
			}); err == nil {
				t.Fatalf("Authorize() succeeded for status %d", status)
			}
			if calls.Load() != 1 {
				t.Fatalf("HTTP calls = %d, want 1", calls.Load())
			}
		})
	}
}

type idTokenSourceFunc func(context.Context, string) (string, error)

func (fn idTokenSourceFunc) Token(ctx context.Context, audience string) (string, error) {
	return fn(ctx, audience)
}
