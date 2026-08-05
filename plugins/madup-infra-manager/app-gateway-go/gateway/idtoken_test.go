package gateway

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

func TestMetadataIDTokenSourceUsesGoogleMetadataContract(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Metadata-Flavor"); got != "Google" {
			t.Fatalf("Metadata-Flavor = %q", got)
		}
		if got := r.URL.Path; got != "/computeMetadata/v1/instance/service-accounts/default/identity" {
			t.Fatalf("Path = %q", got)
		}
		if got := r.URL.Query().Get("audience"); got != "https://upstream.example" {
			t.Fatalf("audience = %q", got)
		}
		if got := r.URL.Query().Get("format"); got != "full" {
			t.Fatalf("format = %q", got)
		}
		_, _ = w.Write([]byte("metadata-token"))
	}))
	defer server.Close()

	source, err := newMetadataIDTokenSource(server.Client(), server.URL)
	if err != nil {
		t.Fatalf("newMetadataIDTokenSource() error = %v", err)
	}

	token, err := source.Token(context.Background(), "https://upstream.example")
	if err != nil {
		t.Fatalf("Token() error = %v", err)
	}
	if token != "metadata-token" {
		t.Fatalf("Token = %q", token)
	}
}

func TestNewMetadataIDTokenSourceUsesFixedMetadataHost(t *testing.T) {
	source := NewMetadataIDTokenSource(http.DefaultClient)
	u, err := url.Parse(source.baseURL)
	if err != nil {
		t.Fatalf("url.Parse() error = %v", err)
	}
	if u.Host != "metadata.google.internal" {
		t.Fatalf("baseURL host = %q", u.Host)
	}
}
