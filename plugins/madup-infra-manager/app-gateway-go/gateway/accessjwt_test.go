package gateway

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestAccessTokenVerifierAcceptsExactRS256JWT(t *testing.T) {
	now := time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC)
	key := mustRSAKey(t)
	jwks := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"keys": []any{jwkFromKey("kid-1", key)},
		})
	}))
	defer jwks.Close()

	verifier := NewAccessTokenVerifier(AccessTokenVerifierConfig{
		Issuer:   "https://madup.cloudflareaccess.com",
		Audience: "cf-aud-app-1234567890",
		JWKSURL:  jwks.URL,
		Now:      func() time.Time { return now },
		Client:   jwks.Client(),
		CacheTTL: time.Minute,
	})
	token := signJWT(t, key, "kid-1", map[string]any{
		"iss":            "https://madup.cloudflareaccess.com",
		"aud":            "cf-aud-app-1234567890",
		"sub":            "user-123",
		"email":          "person@madup.com",
		"email_verified": true,
		"iat":            now.Add(-1 * time.Minute).Unix(),
		"nbf":            now.Add(-1 * time.Minute).Unix(),
		"exp":            now.Add(5 * time.Minute).Unix(),
	})

	claims, err := verifier.Verify(context.Background(), token)
	if err != nil {
		t.Fatalf("Verify() error = %v", err)
	}
	if claims.Subject != "user-123" {
		t.Fatalf("Subject = %q", claims.Subject)
	}
	if claims.Email != "person@madup.com" {
		t.Fatalf("Email = %q", claims.Email)
	}
}

func TestAccessTokenVerifierRefreshesJWKSAndFailsClosed(t *testing.T) {
	now := time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC)
	key1 := mustRSAKey(t)
	key2 := mustRSAKey(t)
	var count atomic.Int32
	var serveInvalid atomic.Bool
	current := "kid-1"
	jwks := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		count.Add(1)
		if serveInvalid.Load() {
			_, _ = w.Write([]byte(`{"keys":[`))
			return
		}
		key := key1
		if current == "kid-2" {
			key = key2
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"keys": []any{jwkFromKey(current, key)},
		})
	}))
	defer jwks.Close()

	verifier := NewAccessTokenVerifier(AccessTokenVerifierConfig{
		Issuer:   "https://madup.cloudflareaccess.com",
		Audience: "cf-aud-app-1234567890",
		JWKSURL:  jwks.URL,
		Now:      func() time.Time { return now },
		Client:   jwks.Client(),
		CacheTTL: time.Hour,
	})

	token1 := signJWT(t, key1, "kid-1", map[string]any{
		"iss":            "https://madup.cloudflareaccess.com",
		"aud":            "cf-aud-app-1234567890",
		"sub":            "user-123",
		"email":          "person@madup.com",
		"email_verified": true,
		"iat":            now.Add(-1 * time.Minute).Unix(),
		"nbf":            now.Add(-1 * time.Minute).Unix(),
		"exp":            now.Add(5 * time.Minute).Unix(),
	})
	if _, err := verifier.Verify(context.Background(), token1); err != nil {
		t.Fatalf("Verify(token1) error = %v", err)
	}

	current = "kid-2"
	token2 := signJWT(t, key2, "kid-2", map[string]any{
		"iss":            "https://madup.cloudflareaccess.com",
		"aud":            "cf-aud-app-1234567890",
		"sub":            "user-456",
		"email":          "person@madup.com",
		"email_verified": true,
		"iat":            now.Add(-1 * time.Minute).Unix(),
		"nbf":            now.Add(-1 * time.Minute).Unix(),
		"exp":            now.Add(5 * time.Minute).Unix(),
	})
	claims, err := verifier.Verify(context.Background(), token2)
	if err != nil {
		t.Fatalf("Verify(token2) error = %v", err)
	}
	if claims.Subject != "user-456" {
		t.Fatalf("Subject = %q", claims.Subject)
	}
	if got := count.Load(); got < 2 {
		t.Fatalf("JWKS fetch count = %d, want at least 2", got)
	}

	serveInvalid.Store(true)
	bad := signJWT(t, key2, "kid-3", map[string]any{
		"iss":            "https://madup.cloudflareaccess.com",
		"aud":            "cf-aud-app-1234567890",
		"sub":            "user-789",
		"email":          "person@madup.com",
		"email_verified": true,
		"iat":            now.Add(-1 * time.Minute).Unix(),
		"nbf":            now.Add(-1 * time.Minute).Unix(),
		"exp":            now.Add(5 * time.Minute).Unix(),
	})
	if _, err := verifier.Verify(context.Background(), bad); err == nil {
		t.Fatal("Verify() succeeded with invalid JWKS response")
	}
}

func TestAccessTokenVerifierRejectsNonRS256AndBadClaims(t *testing.T) {
	now := time.Date(2026, 8, 5, 10, 11, 12, 0, time.UTC)
	key := mustRSAKey(t)
	jwks := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"keys": []any{jwkFromKey("kid-1", key)},
		})
	}))
	defer jwks.Close()

	verifier := NewAccessTokenVerifier(AccessTokenVerifierConfig{
		Issuer:   "https://madup.cloudflareaccess.com",
		Audience: "cf-aud-app-1234567890",
		JWKSURL:  jwks.URL,
		Now:      func() time.Time { return now },
		Client:   jwks.Client(),
		CacheTTL: time.Minute,
	})

	badCases := []string{
		manualJWT(t, map[string]any{"alg": "HS256", "kid": "kid-1", "typ": "JWT"}, map[string]any{
			"iss": "https://madup.cloudflareaccess.com", "aud": "cf-aud-app-1234567890", "sub": "u", "email": "person@madup.com", "email_verified": true,
			"iat": now.Add(-1 * time.Minute).Unix(), "nbf": now.Add(-1 * time.Minute).Unix(), "exp": now.Add(1 * time.Minute).Unix(),
		}, "bogus"),
		signJWT(t, key, "kid-1", map[string]any{
			"iss": "https://other.cloudflareaccess.com", "aud": "cf-aud-app-1234567890", "sub": "u", "email": "person@madup.com", "email_verified": true,
			"iat": now.Add(-1 * time.Minute).Unix(), "nbf": now.Add(-1 * time.Minute).Unix(), "exp": now.Add(1 * time.Minute).Unix(),
		}),
		signJWT(t, key, "kid-1", map[string]any{
			"iss": "https://madup.cloudflareaccess.com", "aud": []any{"cf-aud-app-1234567890", "other"}, "sub": "u", "email": "person@madup.com", "email_verified": true,
			"iat": now.Add(-1 * time.Minute).Unix(), "nbf": now.Add(-1 * time.Minute).Unix(), "exp": now.Add(1 * time.Minute).Unix(),
		}),
		signJWT(t, key, "kid-1", map[string]any{
			"iss": "https://madup.cloudflareaccess.com", "aud": "cf-aud-app-1234567890", "sub": "u", "email": "person@example.com", "email_verified": true,
			"iat": now.Add(-1 * time.Minute).Unix(), "nbf": now.Add(-1 * time.Minute).Unix(), "exp": now.Add(1 * time.Minute).Unix(),
		}),
		signJWT(t, key, "kid-1", map[string]any{
			"iss": "https://madup.cloudflareaccess.com", "aud": "cf-aud-app-1234567890", "sub": "u", "email": "person@madup.com", "email_verified": true,
			"iat": now.Add(1 * time.Minute).Unix(), "nbf": now.Add(-1 * time.Minute).Unix(), "exp": now.Add(1 * time.Minute).Unix(),
		}),
	}

	for index, token := range badCases {
		t.Run(fmt.Sprintf("case-%d", index), func(t *testing.T) {
			if _, err := verifier.Verify(context.Background(), token); err == nil {
				t.Fatal("Verify() succeeded unexpectedly")
			}
		})
	}
}

func mustRSAKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("rsa.GenerateKey() error = %v", err)
	}
	return key
}

func jwkFromKey(kid string, key *rsa.PrivateKey) map[string]any {
	return map[string]any{
		"kty": "RSA",
		"alg": "RS256",
		"use": "sig",
		"kid": kid,
		"n":   base64.RawURLEncoding.EncodeToString(key.N.Bytes()),
		"e":   base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes()),
	}
}

func signJWT(t *testing.T, key *rsa.PrivateKey, kid string, claims map[string]any) string {
	t.Helper()
	header := map[string]any{"alg": "RS256", "kid": kid, "typ": "JWT"}
	headerJSON, err := json.Marshal(header)
	if err != nil {
		t.Fatalf("json.Marshal(header) error = %v", err)
	}
	claimsJSON, err := json.Marshal(claims)
	if err != nil {
		t.Fatalf("json.Marshal(claims) error = %v", err)
	}
	signingInput := base64.RawURLEncoding.EncodeToString(headerJSON) + "." + base64.RawURLEncoding.EncodeToString(claimsJSON)
	sum := sha256.Sum256([]byte(signingInput))
	signature, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, sum[:])
	if err != nil {
		t.Fatalf("rsa.SignPKCS1v15() error = %v", err)
	}
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(signature)
}

func manualJWT(t *testing.T, header map[string]any, claims map[string]any, signature string) string {
	t.Helper()
	headerJSON, err := json.Marshal(header)
	if err != nil {
		t.Fatalf("json.Marshal(header) error = %v", err)
	}
	claimsJSON, err := json.Marshal(claims)
	if err != nil {
		t.Fatalf("json.Marshal(claims) error = %v", err)
	}
	return strings.Join([]string{
		base64.RawURLEncoding.EncodeToString(headerJSON),
		base64.RawURLEncoding.EncodeToString(claimsJSON),
		base64.RawURLEncoding.EncodeToString([]byte(signature)),
	}, ".")
}
