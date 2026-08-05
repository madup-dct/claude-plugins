package gateway

import (
	"context"
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math/big"
	"net/http"
	"strings"
	"sync"
	"time"
)

var errAccessTokenDenied = errors.New("access token denied")

type AccessClaims struct {
	Subject   string
	Email     string
	IssuedAt  time.Time
	NotBefore time.Time
	ExpiresAt time.Time
}

type AccessTokenVerifierConfig struct {
	Issuer   string
	Audience string
	JWKSURL  string
	Client   *http.Client
	Now      func() time.Time
	CacheTTL time.Duration
}

type AccessTokenVerifier struct {
	issuer   string
	audience string
	jwksURL  string
	client   *http.Client
	now      func() time.Time
	cacheTTL time.Duration

	mu         sync.Mutex
	cachedAt   time.Time
	publicKeys map[string]*rsa.PublicKey
}

func NewAccessTokenVerifier(cfg AccessTokenVerifierConfig) *AccessTokenVerifier {
	client := cfg.Client
	if client == nil {
		client = NewControlHTTPClient()
	}
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	cacheTTL := cfg.CacheTTL
	if cacheTTL <= 0 {
		cacheTTL = 5 * time.Minute
	}
	return &AccessTokenVerifier{
		issuer:     cfg.Issuer,
		audience:   cfg.Audience,
		jwksURL:    cfg.JWKSURL,
		client:     client,
		now:        now,
		cacheTTL:   cacheTTL,
		publicKeys: make(map[string]*rsa.PublicKey),
	}
}

func (v *AccessTokenVerifier) Verify(ctx context.Context, token string) (AccessClaims, error) {
	header, payload, signingInput, signature, err := splitJWT(token)
	if err != nil {
		return AccessClaims{}, errAccessTokenDenied
	}
	if header.Alg != "RS256" || header.Kid == "" {
		return AccessClaims{}, errAccessTokenDenied
	}
	key, err := v.lookupKey(ctx, header.Kid)
	if err != nil {
		return AccessClaims{}, errAccessTokenDenied
	}
	sum := sha256.Sum256([]byte(signingInput))
	if err := rsa.VerifyPKCS1v15(key, crypto.SHA256, sum[:], signature); err != nil {
		return AccessClaims{}, errAccessTokenDenied
	}
	return v.validateClaims(payload)
}

type jwtHeader struct {
	Alg string `json:"alg"`
	Kid string `json:"kid"`
	Typ string `json:"typ"`
}

func splitJWT(token string) (jwtHeader, map[string]any, string, []byte, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return jwtHeader{}, nil, "", nil, errAccessTokenDenied
	}
	headerBytes, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return jwtHeader{}, nil, "", nil, err
	}
	payloadBytes, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return jwtHeader{}, nil, "", nil, err
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return jwtHeader{}, nil, "", nil, err
	}
	var header jwtHeader
	if err := json.Unmarshal(headerBytes, &header); err != nil {
		return jwtHeader{}, nil, "", nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal(payloadBytes, &payload); err != nil {
		return jwtHeader{}, nil, "", nil, err
	}
	return header, payload, parts[0] + "." + parts[1], signature, nil
}

func (v *AccessTokenVerifier) lookupKey(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	v.mu.Lock()
	needsRefresh := v.cachedAt.IsZero() || v.now().Sub(v.cachedAt) >= v.cacheTTL
	key := v.publicKeys[kid]
	v.mu.Unlock()
	if key != nil && !needsRefresh {
		return key, nil
	}
	if err := v.refreshKeys(ctx); err != nil {
		return nil, err
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	key = v.publicKeys[kid]
	if key == nil {
		return nil, errAccessTokenDenied
	}
	return key, nil
}

func (v *AccessTokenVerifier) refreshKeys(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, v.jwksURL, nil)
	if err != nil {
		return err
	}
	resp, err := v.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return errAccessTokenDenied
	}
	var payload struct {
		Keys []struct {
			Kty string `json:"kty"`
			Alg string `json:"alg"`
			Use string `json:"use"`
			Kid string `json:"kid"`
			N   string `json:"n"`
			E   string `json:"e"`
		} `json:"keys"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return err
	}
	keys := make(map[string]*rsa.PublicKey, len(payload.Keys))
	for _, item := range payload.Keys {
		if item.Kty != "RSA" || item.Alg != "RS256" || item.Kid == "" {
			continue
		}
		nBytes, err := base64.RawURLEncoding.DecodeString(item.N)
		if err != nil {
			return err
		}
		eBytes, err := base64.RawURLEncoding.DecodeString(item.E)
		if err != nil {
			return err
		}
		eValue := new(big.Int).SetBytes(eBytes)
		if !eValue.IsInt64() {
			return errAccessTokenDenied
		}
		keys[item.Kid] = &rsa.PublicKey{
			N: new(big.Int).SetBytes(nBytes),
			E: int(eValue.Int64()),
		}
	}
	if len(keys) == 0 {
		return errAccessTokenDenied
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	v.publicKeys = keys
	v.cachedAt = v.now()
	return nil
}

func (v *AccessTokenVerifier) validateClaims(payload map[string]any) (AccessClaims, error) {
	iss, _ := payload["iss"].(string)
	sub, _ := payload["sub"].(string)
	email, _ := payload["email"].(string)
	if iss != v.issuer || sub == "" || !strings.HasSuffix(strings.ToLower(email), "@madup.com") || payload["email_verified"] != true {
		return AccessClaims{}, errAccessTokenDenied
	}
	if !audienceMatches(payload["aud"], v.audience) {
		return AccessClaims{}, errAccessTokenDenied
	}
	iat, err := numericDate(payload["iat"])
	if err != nil {
		return AccessClaims{}, errAccessTokenDenied
	}
	nbf, err := numericDate(payload["nbf"])
	if err != nil {
		return AccessClaims{}, errAccessTokenDenied
	}
	exp, err := numericDate(payload["exp"])
	if err != nil {
		return AccessClaims{}, errAccessTokenDenied
	}
	now := v.now().UTC()
	if now.Before(iat) || now.Before(nbf) || !now.Before(exp) {
		return AccessClaims{}, errAccessTokenDenied
	}
	return AccessClaims{
		Subject:   sub,
		Email:     strings.ToLower(email),
		IssuedAt:  iat,
		NotBefore: nbf,
		ExpiresAt: exp,
	}, nil
}

func audienceMatches(value any, expected string) bool {
	switch typed := value.(type) {
	case string:
		return typed == expected
	case []any:
		return len(typed) == 1 && typed[0] == expected
	default:
		return false
	}
}

func numericDate(value any) (time.Time, error) {
	switch typed := value.(type) {
	case float64:
		return time.Unix(int64(typed), 0).UTC(), nil
	case int64:
		return time.Unix(typed, 0).UTC(), nil
	default:
		return time.Time{}, errAccessTokenDenied
	}
}
