package gateway

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"strings"
	"time"
)

var errInvalidDecision = errors.New("authorization decision invalid")
var serviceHostPattern = regexp.MustCompile(`^mim-svc-[0-9a-f]{12}-`)

type proxyHandlerDeps struct {
	Clock       func() time.Time
	TokenSource IDTokenSource
	Transport   http.RoundTripper
}

type proxyHandler struct {
	cfg  Config
	deps proxyHandlerDeps
}

func newProxyHandler(cfg Config, deps proxyHandlerDeps) *proxyHandler {
	if deps.Clock == nil {
		deps.Clock = time.Now
	}
	if deps.Transport == nil {
		deps.Transport = http.DefaultTransport
	}
	return &proxyHandler{cfg: cfg, deps: deps}
}

func ValidateDecisionUpstream(cfg Config, requestedHost string, decision AuthorizationDecision, now time.Time) (*url.URL, error) {
	if decision.PublicHost != requestedHost || !now.UTC().Before(decision.ExpiresAt.UTC()) {
		return nil, errInvalidDecision
	}
	if decision.UpstreamURL != decision.UpstreamAudience {
		return nil, errInvalidDecision
	}
	upstream, err := url.Parse(decision.UpstreamURL)
	if err != nil {
		return nil, errInvalidDecision
	}
	if upstream.Scheme != "https" || upstream.Hostname() == "" || upstream.Port() != "" || upstream.Path != "" || upstream.RawQuery != "" || upstream.Fragment != "" || upstream.User != nil {
		return nil, errInvalidDecision
	}
	host := strings.ToLower(upstream.Hostname())
	if !strings.HasSuffix(host, ".run.app") || !serviceHostPattern.MatchString(host) {
		return nil, errInvalidDecision
	}
	if expectedPrefix := "mim-svc-" + workloadSuffix(decision.WorkloadID) + "-"; !strings.HasPrefix(host, expectedPrefix) {
		return nil, errInvalidDecision
	}
	return upstream, nil
}

func (h *proxyHandler) ServeAuthorizedHTTP(w http.ResponseWriter, req *http.Request, decision AuthorizationDecision, upstreamURL *url.URL, publicHost string) {
	defer func() {
		if recover() != nil {
			writeText(w, http.StatusBadGateway, "Origin unavailable.")
		}
	}()
	token, err := h.deps.TokenSource.Token(req.Context(), decision.UpstreamAudience)
	if err != nil {
		writeText(w, http.StatusBadGateway, "Origin unavailable.")
		return
	}
	proxy := &httputil.ReverseProxy{
		Transport: h.deps.Transport,
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(upstreamURL)
			pr.Out.Host = upstreamURL.Host
			sanitizeForwardHeaders(pr.Out.Header)
			filteredCookie := filterCookies(pr.Out.Header.Get("Cookie"))
			pr.Out.Header.Del("Cookie")
			if filteredCookie != "" {
				pr.Out.Header.Set("Cookie", filteredCookie)
			}
			pr.Out.Header.Set("X-Serverless-Authorization", "Bearer "+token)
			pr.Out.Header.Set("X-Forwarded-Host", publicHost)
			pr.Out.Header.Set("X-Forwarded-Proto", "https")
		},
		ModifyResponse: func(resp *http.Response) error {
			rewriteResponseCookies(resp.Header, publicHost)
			if err := rewriteRedirect(resp.Header, publicHost, upstreamURL.Hostname()); err != nil {
				return err
			}
			return nil
		},
		ErrorHandler: func(rw http.ResponseWriter, r *http.Request, err error) {
			writeText(rw, http.StatusBadGateway, "Origin unavailable.")
		},
	}
	proxy.ServeHTTP(w, req)
}

func sanitizeForwardHeaders(headers http.Header) {
	for _, name := range []string{
		"Authorization",
		"Proxy-Authorization",
		HeaderAccessJWTAssertion,
		HeaderOriginKeyID,
		HeaderOriginTimestamp,
		HeaderOriginRequestID,
		HeaderOriginPublicHost,
		HeaderOriginDestinationClass,
		HeaderOriginSignature,
		"Forwarded",
		"X-Forwarded-For",
		"X-Forwarded-Host",
		"X-Forwarded-Proto",
		"X-Real-IP",
		"True-Client-IP",
		"CDN-Loop",
	} {
		headers.Del(name)
	}
	for name := range headers {
		lower := strings.ToLower(name)
		if strings.HasPrefix(lower, "cf-") || strings.HasPrefix(lower, "x-mim-") {
			headers.Del(name)
		}
	}
}

func filterCookies(raw string) string {
	if strings.TrimSpace(raw) == "" {
		return ""
	}
	parts := strings.Split(raw, ";")
	kept := make([]string, 0, len(parts))
	for _, part := range parts {
		item := strings.TrimSpace(part)
		if item == "" {
			continue
		}
		name, _, _ := strings.Cut(item, "=")
		lower := strings.ToLower(strings.TrimSpace(name))
		if lower == "cf_authorization" || lower == "__host-cf_authorization" || lower == "__secure-cf_authorization" || strings.HasPrefix(lower, "mim_") || strings.HasPrefix(lower, "__host-mim_") || strings.HasPrefix(lower, "__secure-mim_") {
			continue
		}
		kept = append(kept, item)
	}
	return strings.Join(kept, "; ")
}

func rewriteResponseCookies(headers http.Header, publicHost string) {
	values := headers.Values("Set-Cookie")
	if len(values) == 0 {
		return
	}
	headers.Del("Set-Cookie")
	for _, value := range values {
		if rewritten, ok := rewriteSetCookieDomain(value, publicHost); ok {
			headers.Add("Set-Cookie", rewritten)
		}
	}
}

func rewriteSetCookieDomain(raw, publicHost string) (string, bool) {
	parts := strings.Split(raw, ";")
	if len(parts) == 0 {
		return "", false
	}
	rewritten := make([]string, 0, len(parts))
	for index, part := range parts {
		trimmed := strings.TrimSpace(part)
		if index == 0 {
			rewritten = append(rewritten, trimmed)
			continue
		}
		name, value, hasValue := strings.Cut(trimmed, "=")
		if !hasValue || !strings.EqualFold(strings.TrimSpace(name), "Domain") {
			rewritten = append(rewritten, trimmed)
			continue
		}
		domain := strings.TrimPrefix(strings.ToLower(strings.TrimSpace(value)), ".")
		switch {
		case domain == strings.ToLower(publicHost), domain == "madup.app", domain == "run.app", strings.HasSuffix(domain, ".run.app"):
			rewritten = append(rewritten, "Domain="+publicHost)
		default:
			return "", false
		}
	}
	return strings.Join(rewritten, "; "), true
}

func rewriteRedirect(headers http.Header, publicHost, upstreamHost string) error {
	location := headers.Get("Location")
	if location == "" {
		return nil
	}
	parsed, err := url.Parse(location)
	if err != nil || !parsed.IsAbs() {
		return nil
	}
	host := strings.ToLower(parsed.Hostname())
	if strings.HasSuffix(host, ".run.app") {
		if host != strings.ToLower(upstreamHost) {
			return errInvalidDecision
		}
		parsed.Scheme = "https"
		parsed.Host = publicHost
		headers.Set("Location", parsed.String())
	}
	return nil
}

func writeText(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(body))
}

func contextNow(ctx context.Context, fallback func() time.Time) time.Time {
	if value, ok := ctx.Value(ctxKeyNow{}).(time.Time); ok && !value.IsZero() {
		return value.UTC()
	}
	return fallback().UTC()
}

func workloadSuffix(workloadID string) string {
	sum := sha256.Sum256([]byte(workloadID))
	return hex.EncodeToString(sum[:])[:12]
}
