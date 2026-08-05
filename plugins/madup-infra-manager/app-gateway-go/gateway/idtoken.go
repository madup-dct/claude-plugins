package gateway

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
)

const metadataBaseURL = "http://metadata.google.internal"

type MetadataIDTokenSource struct {
	client  *http.Client
	baseURL string
}

func NewMetadataIDTokenSource(client *http.Client) *MetadataIDTokenSource {
	source, _ := newMetadataIDTokenSource(client, metadataBaseURL)
	return source
}

func newMetadataIDTokenSource(client *http.Client, baseURL string) (*MetadataIDTokenSource, error) {
	if client == nil {
		client = NewControlHTTPClient()
	}
	parsed, err := url.Parse(baseURL)
	if err != nil {
		return nil, err
	}
	if parsed.Scheme == "" || parsed.Host == "" {
		return nil, errInvalidConfig
	}
	return &MetadataIDTokenSource{
		client:  client,
		baseURL: strings.TrimRight(baseURL, "/"),
	}, nil
}

func (s *MetadataIDTokenSource) Token(ctx context.Context, audience string) (string, error) {
	endpoint := s.baseURL + "/computeMetadata/v1/instance/service-accounts/default/identity"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return "", err
	}
	query := req.URL.Query()
	query.Set("audience", audience)
	query.Set("format", "full")
	req.URL.RawQuery = query.Encode()
	req.Header.Set("Metadata-Flavor", "Google")
	resp, err := s.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", errors.New("metadata token request failed")
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	token := strings.TrimSpace(string(body))
	if token == "" {
		return "", errors.New("metadata token request failed")
	}
	return token, nil
}
