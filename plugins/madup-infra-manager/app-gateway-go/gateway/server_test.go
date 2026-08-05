package gateway

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestServerAuthorizesBeforeMintingUpstreamTokenOrProxying(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	var authzCalls atomic.Int32
	var tokenCalls atomic.Int32
	var proxyCalls atomic.Int32

	handler, err := NewServer(cfg, ServerDeps{
		Clock: func() time.Time { return time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC) },
		AccessVerifier: accessVerifierFunc(func(ctx context.Context, token string) (AccessClaims, error) {
			return AccessClaims{Subject: "user-123", Email: "person@madup.com"}, nil
		}),
		Authorizer: authorizerFunc(func(ctx context.Context, req AuthorizationRequest) (AuthorizationDecision, error) {
			authzCalls.Add(1)
			return AuthorizationDecision{}, errAuthorizationDenied
		}),
		UpstreamTokenSource: idTokenSourceFunc(func(ctx context.Context, audience string) (string, error) {
			tokenCalls.Add(1)
			return "never", nil
		}),
		Transport: roundTripperFunc(func(req *http.Request) (*http.Response, error) {
			proxyCalls.Add(1)
			return nil, errors.New("unexpected")
		}),
	})
	if err != nil {
		t.Fatalf("NewServer() error = %v", err)
	}

	now := time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC)
	body := []byte(`{}`)
	req := httptest.NewRequest(http.MethodGet, "https://mim-app-gateway-123456789012.asia-northeast3.run.app/path", strings.NewReader(string(body)))
	req.Header.Set(HeaderAccessJWTAssertion, "jwt")
	req.Header.Set(HeaderOriginKeyID, cfg.CurrentProofKeyID)
	req.Header.Set(HeaderOriginTimestamp, "1785924672")
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
		"1785924672",
		"11111111-2222-4333-8444-555555555555",
		cfg.CurrentProofKeyID,
	}))
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, req.WithContext(context.WithValue(req.Context(), ctxKeyNow{}, now)))

	if authzCalls.Load() != 1 {
		t.Fatalf("authz calls = %d", authzCalls.Load())
	}
	if tokenCalls.Load() != 0 {
		t.Fatalf("token calls = %d", tokenCalls.Load())
	}
	if proxyCalls.Load() != 0 {
		t.Fatalf("proxy calls = %d", proxyCalls.Load())
	}
	if recorder.Code != http.StatusNotFound {
		t.Fatalf("StatusCode = %d", recorder.Code)
	}
}

func TestServerRecoversToGenericError(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}

	handler, err := NewServer(cfg, ServerDeps{
		Clock: func() time.Time { return time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC) },
		AccessVerifier: accessVerifierFunc(func(ctx context.Context, token string) (AccessClaims, error) {
			return AccessClaims{Subject: "user-123", Email: "person@madup.com"}, nil
		}),
		Authorizer: authorizerFunc(func(ctx context.Context, req AuthorizationRequest) (AuthorizationDecision, error) {
			panic("secret-run.app")
		}),
		UpstreamTokenSource: idTokenSourceFunc(func(ctx context.Context, audience string) (string, error) {
			return "never", nil
		}),
		Transport: roundTripperFunc(func(req *http.Request) (*http.Response, error) {
			return nil, errors.New("unexpected")
		}),
	})
	if err != nil {
		t.Fatalf("NewServer() error = %v", err)
	}

	body := []byte(`{}`)
	req := httptest.NewRequest(http.MethodGet, "https://mim-app-gateway-123456789012.asia-northeast3.run.app/path", strings.NewReader(string(body)))
	req.Header.Set(HeaderAccessJWTAssertion, "jwt")
	req.Header.Set(HeaderOriginKeyID, cfg.CurrentProofKeyID)
	req.Header.Set(HeaderOriginTimestamp, "1785924672")
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
		"1785924672",
		"11111111-2222-4333-8444-555555555555",
		cfg.CurrentProofKeyID,
	}))
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("StatusCode = %d", recorder.Code)
	}
	if body := recorder.Body.String(); strings.Contains(body, "secret-run.app") {
		t.Fatalf("panic leaked sensitive material: %q", body)
	}
}

func TestServerHealthEndpointsBypassProof(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	handler, err := NewServer(cfg, ServerDeps{
		Clock:          func() time.Time { return time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC) },
		AccessVerifier: accessVerifierFunc(func(ctx context.Context, token string) (AccessClaims, error) { return AccessClaims{}, nil }),
		Authorizer: authorizerFunc(func(ctx context.Context, req AuthorizationRequest) (AuthorizationDecision, error) {
			return AuthorizationDecision{}, nil
		}),
		UpstreamTokenSource: idTokenSourceFunc(func(ctx context.Context, audience string) (string, error) { return "", nil }),
		Transport:           roundTripperFunc(func(req *http.Request) (*http.Response, error) { return nil, nil }),
	})
	if err != nil {
		t.Fatalf("NewServer() error = %v", err)
	}

	for _, path := range []string{"/healthz", "/readyz"} {
		req := httptest.NewRequest(http.MethodGet, "https://mim-app-gateway-123456789012.asia-northeast3.run.app"+path, nil)
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, req)
		if recorder.Code != http.StatusOK {
			t.Fatalf("%s status = %d", path, recorder.Code)
		}
	}
}

func TestServerDeniesDirectRunAppRequestWithoutProof(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	handler, err := NewServer(cfg, ServerDeps{
		Clock:          func() time.Time { return time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC) },
		AccessVerifier: accessVerifierFunc(func(ctx context.Context, token string) (AccessClaims, error) { return AccessClaims{}, nil }),
		Authorizer: authorizerFunc(func(ctx context.Context, req AuthorizationRequest) (AuthorizationDecision, error) {
			return AuthorizationDecision{}, nil
		}),
		UpstreamTokenSource: idTokenSourceFunc(func(ctx context.Context, audience string) (string, error) { return "", nil }),
		Transport:           roundTripperFunc(func(req *http.Request) (*http.Response, error) { return nil, nil }),
	})
	if err != nil {
		t.Fatalf("NewServer() error = %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "https://mim-app-gateway-123456789012.asia-northeast3.run.app/", nil)
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)
	if recorder.Code != http.StatusForbidden {
		t.Fatalf("StatusCode = %d", recorder.Code)
	}
	if body := recorder.Body.String(); body != "Request denied." {
		t.Fatalf("Body = %q", body)
	}
}

type accessVerifierFunc func(context.Context, string) (AccessClaims, error)

func (fn accessVerifierFunc) Verify(ctx context.Context, token string) (AccessClaims, error) {
	return fn(ctx, token)
}

type authorizerFunc func(context.Context, AuthorizationRequest) (AuthorizationDecision, error)

func (fn authorizerFunc) Authorize(ctx context.Context, req AuthorizationRequest) (AuthorizationDecision, error) {
	return fn(ctx, req)
}
