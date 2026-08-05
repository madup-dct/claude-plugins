package gateway

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"
)

type AccessVerifier interface {
	Verify(context.Context, string) (AccessClaims, error)
}

type Authorizer interface {
	Authorize(context.Context, AuthorizationRequest) (AuthorizationDecision, error)
}

type ServerDeps struct {
	Clock               func() time.Time
	AccessVerifier      AccessVerifier
	Authorizer          Authorizer
	UpstreamTokenSource IDTokenSource
	Transport           http.RoundTripper
}

type ctxKeyNow struct{}

type server struct {
	cfg   Config
	deps  ServerDeps
	proxy *proxyHandler
}

func NewServer(cfg Config, deps ServerDeps) (http.Handler, error) {
	if deps.Clock == nil {
		deps.Clock = time.Now
	}
	if deps.AccessVerifier == nil || deps.Authorizer == nil || deps.UpstreamTokenSource == nil {
		return nil, errInvalidConfig
	}
	return &server{
		cfg:  cfg,
		deps: deps,
		proxy: newProxyHandler(cfg, proxyHandlerDeps{
			Clock:       deps.Clock,
			TokenSource: deps.UpstreamTokenSource,
			Transport:   deps.Transport,
		}),
	}, nil
}

func (s *server) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	defer func() {
		if recover() != nil {
			writeText(w, http.StatusBadGateway, "Origin unavailable.")
		}
	}()
	if req.Method == http.MethodGet && (req.URL.Path == "/healthz" || req.URL.Path == "/readyz") {
		writeText(w, http.StatusOK, "ok")
		return
	}
	body, err := readBody(req, s.cfg.RequestBodyLimit)
	if err != nil {
		writeText(w, http.StatusRequestEntityTooLarge, "Payload too large.")
		return
	}
	now := contextNow(req.Context(), s.deps.Clock)
	proof, err := VerifyEdgeProof(req, body, s.cfg, now)
	if err != nil {
		writeText(w, http.StatusForbidden, "Request denied.")
		return
	}
	assertion, err := singleAccessAssertion(req.Header)
	if err != nil {
		writeText(w, http.StatusForbidden, "Request denied.")
		return
	}
	claims, err := s.deps.AccessVerifier.Verify(req.Context(), assertion)
	if err != nil {
		writeText(w, http.StatusForbidden, "Request denied.")
		return
	}
	decision, err := s.deps.Authorizer.Authorize(req.Context(), AuthorizationRequest{
		PublicHost:     proof.PublicHost,
		Method:         req.Method,
		RequestTarget:  proof.RequestTarget,
		AccessSubject:  claims.Subject,
		AccessEmail:    claims.Email,
		EdgeRequestID:  proof.RequestID,
		EdgeTimestamp:  proof.Timestamp,
		EdgeBodySHA256: proof.BodySHA256,
	})
	if err != nil {
		if errors.Is(err, errAuthorizationDenied) {
			writeText(w, http.StatusNotFound, "Route not available.")
			return
		}
		writeText(w, http.StatusBadGateway, "Origin unavailable.")
		return
	}
	upstreamURL, err := ValidateDecisionUpstream(s.cfg, proof.PublicHost, decision, now)
	if err != nil {
		writeText(w, http.StatusNotFound, "Route not available.")
		return
	}
	req.Body = io.NopCloser(bytes.NewReader(body))
	s.proxy.ServeAuthorizedHTTP(w, req, decision, upstreamURL, proof.PublicHost)
}

func readBody(req *http.Request, limit int64) ([]byte, error) {
	if req.Body == nil {
		return nil, nil
	}
	defer req.Body.Close()
	limited := io.LimitReader(req.Body, limit+1)
	body, err := io.ReadAll(limited)
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, errors.New("payload too large")
	}
	return body, nil
}

func singleAccessAssertion(headers http.Header) (string, error) {
	values := headers.Values(HeaderAccessJWTAssertion)
	if len(values) != 1 {
		return "", errAccessTokenDenied
	}
	value := strings.TrimSpace(values[0])
	if value == "" {
		return "", errAccessTokenDenied
	}
	return value, nil
}
