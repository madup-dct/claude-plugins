package gateway

import (
	"net/http"
	"testing"
	"time"
)

func TestNewHTTPServerSetsBoundedReadTimeouts(t *testing.T) {
	cfg, err := LoadConfigFromMap(validConfigEnv())
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {})

	server := NewHTTPServer(cfg, handler)

	if server.Addr != cfg.ListenAddr {
		t.Fatalf("Addr = %q, want %q", server.Addr, cfg.ListenAddr)
	}
	if server.Handler == nil {
		t.Fatalf("Handler is nil")
	}
	if server.ReadHeaderTimeout != 10*time.Second {
		t.Fatalf("ReadHeaderTimeout = %s", server.ReadHeaderTimeout)
	}
	if server.ReadTimeout != 15*time.Second {
		t.Fatalf("ReadTimeout = %s", server.ReadTimeout)
	}
	if server.ReadTimeout <= server.ReadHeaderTimeout {
		t.Fatalf("ReadTimeout = %s, ReadHeaderTimeout = %s", server.ReadTimeout, server.ReadHeaderTimeout)
	}
}

func TestNewControlHTTPClientUsesDedicatedShortTimeoutTransport(t *testing.T) {
	client := NewControlHTTPClient()

	if client == nil {
		t.Fatal("client is nil")
	}
	if client == http.DefaultClient {
		t.Fatal("client unexpectedly reuses http.DefaultClient")
	}
	if client.Timeout != 5*time.Second {
		t.Fatalf("Timeout = %s", client.Timeout)
	}
	transport, ok := client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("Transport type = %T", client.Transport)
	}
	if transport == http.DefaultTransport {
		t.Fatal("transport unexpectedly reuses http.DefaultTransport")
	}
	if transport.ResponseHeaderTimeout != 5*time.Second {
		t.Fatalf("ResponseHeaderTimeout = %s", transport.ResponseHeaderTimeout)
	}
	if transport.ExpectContinueTimeout != 1*time.Second {
		t.Fatalf("ExpectContinueTimeout = %s", transport.ExpectContinueTimeout)
	}
	if transport.TLSHandshakeTimeout != 5*time.Second {
		t.Fatalf("TLSHandshakeTimeout = %s", transport.TLSHandshakeTimeout)
	}
}
