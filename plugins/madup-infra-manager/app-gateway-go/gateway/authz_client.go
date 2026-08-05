package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"
)

var errAuthorizationDenied = errors.New("authorization denied")

type IDTokenSource interface {
	Token(context.Context, string) (string, error)
}

type AuthorizationClientConfig struct {
	URL         string
	Audience    string
	TokenSource IDTokenSource
	Client      *http.Client
}

type AuthorizationClient struct {
	url         string
	audience    string
	tokenSource IDTokenSource
	client      *http.Client
}

type AuthorizationRequest struct {
	PublicHost     string
	Method         string
	RequestTarget  string
	AccessSubject  string
	AccessEmail    string
	EdgeRequestID  string
	EdgeTimestamp  time.Time
	EdgeBodySHA256 string
}

type AuthorizationDecision struct {
	PublicHost       string
	WorkloadID       string
	UpstreamURL      string
	UpstreamAudience string
	ExpiresAt        time.Time
}

func NewAuthorizationClient(cfg AuthorizationClientConfig) (*AuthorizationClient, error) {
	if cfg.URL == "" || cfg.Audience == "" || cfg.TokenSource == nil {
		return nil, errInvalidConfig
	}
	client := cfg.Client
	if client == nil {
		client = NewControlHTTPClient()
	}
	return &AuthorizationClient{
		url:         cfg.URL,
		audience:    cfg.Audience,
		tokenSource: cfg.TokenSource,
		client:      client,
	}, nil
}

func (c *AuthorizationClient) Authorize(ctx context.Context, req AuthorizationRequest) (AuthorizationDecision, error) {
	token, err := c.tokenSource.Token(ctx, c.audience)
	if err != nil {
		return AuthorizationDecision{}, err
	}
	payload := map[string]any{
		"schema":           "mim.app-authorization.v1",
		"public_host":      req.PublicHost,
		"method":           req.Method,
		"request_target":   req.RequestTarget,
		"access_subject":   req.AccessSubject,
		"access_email":     req.AccessEmail,
		"edge_request_id":  req.EdgeRequestID,
		"edge_timestamp":   req.EdgeTimestamp.Unix(),
		"edge_body_sha256": req.EdgeBodySHA256,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return AuthorizationDecision{}, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return AuthorizationDecision{}, err
	}
	httpReq.Header.Set("Authorization", "Bearer "+token)
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := c.client.Do(httpReq)
	if err != nil {
		return AuthorizationDecision{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		if resp.StatusCode == http.StatusForbidden || resp.StatusCode == http.StatusNotFound {
			return AuthorizationDecision{}, errAuthorizationDenied
		}
		return AuthorizationDecision{}, errors.New("authorization service unavailable")
	}

	var decoded struct {
		Schema           string `json:"schema"`
		PublicHost       string `json:"public_host"`
		WorkloadID       string `json:"workload_id"`
		UpstreamURL      string `json:"upstream_url"`
		UpstreamAudience string `json:"upstream_audience"`
		ExpiresAt        string `json:"expires_at"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&decoded); err != nil {
		return AuthorizationDecision{}, errors.New("authorization service unavailable")
	}
	if decoded.Schema != "mim.app-authorization.v1" || decoded.PublicHost == "" || decoded.WorkloadID == "" || decoded.UpstreamURL == "" || decoded.UpstreamAudience == "" {
		return AuthorizationDecision{}, errors.New("authorization service unavailable")
	}
	expiresAt, err := time.Parse(time.RFC3339, decoded.ExpiresAt)
	if err != nil {
		return AuthorizationDecision{}, errors.New("authorization service unavailable")
	}
	return AuthorizationDecision{
		PublicHost:       decoded.PublicHost,
		WorkloadID:       decoded.WorkloadID,
		UpstreamURL:      decoded.UpstreamURL,
		UpstreamAudience: decoded.UpstreamAudience,
		ExpiresAt:        expiresAt.UTC(),
	}, nil
}
