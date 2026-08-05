package gateway

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestVerifyEdgeProofAcceptsExactV2Message(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	now := time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC)
	body := []byte(`{"hello":"world"}`)
	req := httptest.NewRequest(http.MethodPost, "https://mim-app-gateway-123456789012.asia-northeast3.run.app/path?query=value", bytes.NewReader(body))
	req.Header.Set(HeaderOriginKeyID, cfg.CurrentProofKeyID)
	req.Header.Set(HeaderOriginTimestamp, strconv.FormatInt(now.Unix(), 10))
	req.Header.Set(HeaderOriginRequestID, "11111111-2222-4333-8444-555555555555")
	req.Header.Set(HeaderOriginPublicHost, "sample-a1b2c3d4e5f6.madup.app")
	req.Header.Set(HeaderOriginDestinationClass, DestinationClassAppGateway)
	req.Header.Set(HeaderOriginSignature, signProof(t, cfg.CurrentProofSecret, []string{
		"mim-origin-v2",
		DestinationClassAppGateway,
		http.MethodPost,
		"sample-a1b2c3d4e5f6.madup.app",
		"/path?query=value",
		sha256Hex(body),
		strconv.FormatInt(now.Unix(), 10),
		"11111111-2222-4333-8444-555555555555",
		cfg.CurrentProofKeyID,
	}))

	proof, err := VerifyEdgeProof(req, body, cfg, now)
	if err != nil {
		t.Fatalf("VerifyEdgeProof() error = %v", err)
	}
	if proof.PublicHost != "sample-a1b2c3d4e5f6.madup.app" {
		t.Fatalf("PublicHost = %q", proof.PublicHost)
	}
	if proof.RequestTarget != "/path?query=value" {
		t.Fatalf("RequestTarget = %q", proof.RequestTarget)
	}
	if proof.BodySHA256 != sha256Hex(body) {
		t.Fatalf("BodySHA256 = %q", proof.BodySHA256)
	}
}

func TestVerifyEdgeProofPreservesWellFormedEscapedTargets(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	now := time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC)
	body := []byte(`{"hello":"world"}`)
	target := "/path%2Fsegment?q=a%20b&next=%2Fdashboard"
	req := httptest.NewRequest(http.MethodGet, "https://mim-app-gateway-123456789012.asia-northeast3.run.app"+target, bytes.NewReader(body))
	req.Header.Set(HeaderOriginKeyID, cfg.CurrentProofKeyID)
	req.Header.Set(HeaderOriginTimestamp, strconv.FormatInt(now.Unix(), 10))
	req.Header.Set(HeaderOriginRequestID, "11111111-2222-4333-8444-555555555555")
	req.Header.Set(HeaderOriginPublicHost, "sample-a1b2c3d4e5f6.madup.app")
	req.Header.Set(HeaderOriginDestinationClass, DestinationClassAppGateway)
	req.Header.Set(HeaderOriginSignature, signProof(t, cfg.CurrentProofSecret, []string{
		"mim-origin-v2",
		DestinationClassAppGateway,
		http.MethodGet,
		"sample-a1b2c3d4e5f6.madup.app",
		target,
		sha256Hex(body),
		strconv.FormatInt(now.Unix(), 10),
		"11111111-2222-4333-8444-555555555555",
		cfg.CurrentProofKeyID,
	}))

	proof, err := VerifyEdgeProof(req, body, cfg, now)
	if err != nil {
		t.Fatalf("VerifyEdgeProof() error = %v", err)
	}
	if proof.RequestTarget != target {
		t.Fatalf("RequestTarget = %q, want %q", proof.RequestTarget, target)
	}
}

func TestVerifyEdgeProofRejectsDriftAndInvalidIDs(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	now := time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC)
	body := []byte(`{"hello":"world"}`)

	makeRequest := func() *http.Request {
		req := httptest.NewRequest(http.MethodGet, "https://mim-app-gateway-123456789012.asia-northeast3.run.app/path", bytes.NewReader(body))
		req.Header.Set(HeaderOriginKeyID, cfg.CurrentProofKeyID)
		req.Header.Set(HeaderOriginTimestamp, strconv.FormatInt(now.Unix(), 10))
		req.Header.Set(HeaderOriginRequestID, "11111111-2222-4333-8444-555555555555")
		req.Header.Set(HeaderOriginPublicHost, "sample-a1b2c3d4e5f6.madup.app")
		req.Header.Set(HeaderOriginDestinationClass, DestinationClassAppGateway)
		req.Header.Set(HeaderOriginSignature, signProof(t, cfg.CurrentProofSecret, []string{
			"mim-origin-v2",
			DestinationClassAppGateway,
			http.MethodGet,
			"sample-a1b2c3d4e5f6.madup.app",
			"/path",
			sha256Hex(body),
			strconv.FormatInt(now.Unix(), 10),
			"11111111-2222-4333-8444-555555555555",
			cfg.CurrentProofKeyID,
		}))
		return req
	}

	cases := []struct {
		name   string
		mutate func(*http.Request, *[]byte, *time.Time)
	}{
		{
			name: "stale timestamp",
			mutate: func(_ *http.Request, _ *[]byte, current *time.Time) {
				*current = now.Add(61 * time.Second)
			},
		},
		{
			name: "future timestamp",
			mutate: func(_ *http.Request, _ *[]byte, current *time.Time) {
				*current = now.Add(-1 * time.Second)
			},
		},
		{
			name: "body drift",
			mutate: func(_ *http.Request, body *[]byte, _ *time.Time) {
				*body = []byte(`{"hello":"mutated"}`)
			},
		},
		{
			name: "path drift",
			mutate: func(req *http.Request, _ *[]byte, _ *time.Time) {
				req.URL.Path = "/other"
				req.RequestURI = "/other"
			},
		},
		{
			name: "host drift",
			mutate: func(req *http.Request, _ *[]byte, _ *time.Time) {
				req.Host = "other-gateway-123456789012.asia-northeast3.run.app"
			},
		},
		{
			name: "class drift",
			mutate: func(req *http.Request, _ *[]byte, _ *time.Time) {
				req.Header.Set(HeaderOriginDestinationClass, "control-plane")
			},
		},
		{
			name: "invalid request id",
			mutate: func(req *http.Request, _ *[]byte, _ *time.Time) {
				req.Header.Set(HeaderOriginRequestID, strings.Repeat("x", 129))
			},
		},
		{
			name: "malformed percent escape",
			mutate: func(req *http.Request, _ *[]byte, _ *time.Time) {
				req.URL.Path = "/bad%zz"
				req.URL.RawPath = "/bad%zz"
				req.RequestURI = "/bad%zz"
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := makeRequest()
			bodyCopy := append([]byte(nil), body...)
			current := now
			tc.mutate(req, &bodyCopy, &current)
			if _, err := VerifyEdgeProof(req, bodyCopy, cfg, current); err == nil {
				t.Fatalf("VerifyEdgeProof() succeeded for %s", tc.name)
			}
		})
	}
}

func signProof(t *testing.T, secret []byte, lines []string) string {
	t.Helper()
	mac := hmac.New(sha256.New, secret)
	if _, err := mac.Write([]byte(strings.Join(lines, "\n"))); err != nil {
		t.Fatalf("mac.Write() error = %v", err)
	}
	return hex.EncodeToString(mac.Sum(nil))
}

func sha256Hex(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}
