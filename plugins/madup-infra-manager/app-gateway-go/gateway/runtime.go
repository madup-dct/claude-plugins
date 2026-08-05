package gateway

import (
	"net"
	"net/http"
	"time"
)

const (
	controlClientTimeout         = 5 * time.Second
	controlResponseHeaderTimeout = 5 * time.Second
	controlTLSHandshakeTimeout   = 5 * time.Second
	controlExpectContinueTimeout = 1 * time.Second
	defaultReadHeaderTimeout     = 10 * time.Second
	defaultReadTimeout           = 15 * time.Second
)

func NewControlHTTPClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyFromEnvironment
	transport.DialContext = (&net.Dialer{
		Timeout:   controlClientTimeout,
		KeepAlive: 30 * time.Second,
	}).DialContext
	transport.ResponseHeaderTimeout = controlResponseHeaderTimeout
	transport.TLSHandshakeTimeout = controlTLSHandshakeTimeout
	transport.ExpectContinueTimeout = controlExpectContinueTimeout

	return &http.Client{
		Timeout:   controlClientTimeout,
		Transport: transport,
	}
}

func NewHTTPServer(cfg Config, handler http.Handler) *http.Server {
	return &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           handler,
		ReadHeaderTimeout: defaultReadHeaderTimeout,
		ReadTimeout:       defaultReadTimeout,
	}
}
