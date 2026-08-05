package gateway

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	HeaderOriginKeyID            = "X-MIM-Origin-Key-Id"
	HeaderOriginTimestamp        = "X-MIM-Origin-Timestamp"
	HeaderOriginRequestID        = "X-MIM-Origin-Request-Id"
	HeaderOriginPublicHost       = "X-MIM-Origin-Public-Host"
	HeaderOriginDestinationClass = "X-MIM-Origin-Destination-Class"
	HeaderOriginSignature        = "X-MIM-Origin-Signature"
	HeaderAccessJWTAssertion     = "Cf-Access-Jwt-Assertion"

	DestinationClassAppGateway = "app-gateway"
)

var (
	errOriginDenied        = errors.New("request denied")
	signaturePattern       = regexp.MustCompile(`^[0-9a-f]{64}$`)
	requestIDPattern       = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)
	publicHostLabelPattern = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
	requestTargetPattern   = regexp.MustCompile(`^/[A-Za-z0-9._~!$&'()*+,;=:@/?%:-]*$`)
)

type EdgeProof struct {
	PublicHost    string
	RequestTarget string
	BodySHA256    string
	RequestID     string
	KeyID         string
	Timestamp     time.Time
}

func VerifyEdgeProof(req *http.Request, body []byte, cfg Config, now time.Time) (EdgeProof, error) {
	if req == nil {
		return EdgeProof{}, errOriginDenied
	}
	publicHost, err := singleHeader(req.Header, HeaderOriginPublicHost)
	if err != nil {
		return EdgeProof{}, err
	}
	publicHost = strings.ToLower(publicHost)
	if !isValidPublicHost(publicHost, cfg.PublicSuffix) {
		return EdgeProof{}, errOriginDenied
	}
	if !strings.EqualFold(hostWithoutPort(req.Host), cfg.GatewayOriginHost) {
		return EdgeProof{}, errOriginDenied
	}
	destinationClass, err := singleHeader(req.Header, HeaderOriginDestinationClass)
	if err != nil || destinationClass != DestinationClassAppGateway {
		return EdgeProof{}, errOriginDenied
	}
	keyID, err := singleHeader(req.Header, HeaderOriginKeyID)
	if err != nil || !keyIDPattern.MatchString(keyID) {
		return EdgeProof{}, errOriginDenied
	}
	requestID, err := singleHeader(req.Header, HeaderOriginRequestID)
	if err != nil || !requestIDPattern.MatchString(requestID) {
		return EdgeProof{}, errOriginDenied
	}
	signature, err := singleHeader(req.Header, HeaderOriginSignature)
	if err != nil || !signaturePattern.MatchString(signature) {
		return EdgeProof{}, errOriginDenied
	}
	timestampValue, err := singleHeader(req.Header, HeaderOriginTimestamp)
	if err != nil {
		return EdgeProof{}, errOriginDenied
	}
	seconds, err := strconv.ParseInt(timestampValue, 10, 64)
	if err != nil {
		return EdgeProof{}, errOriginDenied
	}
	timestamp := time.Unix(seconds, 0).UTC()
	if now.UTC().Before(timestamp) || now.UTC().Sub(timestamp) > 60*time.Second {
		return EdgeProof{}, errOriginDenied
	}
	requestTarget := canonicalRequestTarget(req.URL)
	bodyDigest := bodySHA256(body)
	key, ok := cfg.proofKeys[keyID]
	if !ok {
		return EdgeProof{}, errOriginDenied
	}
	expected := signEdgeProofMessage(key, []string{
		"mim-origin-v2",
		DestinationClassAppGateway,
		req.Method,
		publicHost,
		requestTarget,
		bodyDigest,
		strconv.FormatInt(timestamp.Unix(), 10),
		requestID,
		keyID,
	})
	if !hmac.Equal([]byte(expected), []byte(signature)) {
		return EdgeProof{}, errOriginDenied
	}
	return EdgeProof{
		PublicHost:    publicHost,
		RequestTarget: requestTarget,
		BodySHA256:    bodyDigest,
		RequestID:     requestID,
		KeyID:         keyID,
		Timestamp:     timestamp,
	}, nil
}

func canonicalRequestTarget(rawURL *url.URL) string {
	if rawURL == nil {
		return ""
	}
	target := rawURL.EscapedPath()
	if target == "" {
		target = "/"
	}
	if rawURL.RawQuery != "" {
		target += "?" + rawURL.RawQuery
	}
	if strings.ContainsAny(target, "\\#") || strings.Count(target, "?") > 1 {
		return ""
	}
	for _, ch := range target {
		if ch < 0x21 || ch > 0x7e {
			return ""
		}
	}
	if !requestTargetPattern.MatchString(target) || !validPercentEscapes(target) {
		return ""
	}
	return target
}

func validPercentEscapes(value string) bool {
	for index := 0; index < len(value); index++ {
		if value[index] != '%' {
			continue
		}
		if index+2 >= len(value) || !isHex(value[index+1]) || !isHex(value[index+2]) {
			return false
		}
		index += 2
	}
	return true
}

func isHex(value byte) bool {
	return value >= '0' && value <= '9' || value >= 'a' && value <= 'f' || value >= 'A' && value <= 'F'
}

func bodySHA256(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

func signEdgeProofMessage(secret []byte, lines []string) string {
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write([]byte(strings.Join(lines, "\n")))
	return hex.EncodeToString(mac.Sum(nil))
}

func singleHeader(headers http.Header, name string) (string, error) {
	values := headers.Values(name)
	if len(values) != 1 {
		return "", errOriginDenied
	}
	value := strings.TrimSpace(values[0])
	if value == "" {
		return "", errOriginDenied
	}
	return value, nil
}

func isValidPublicHost(host, suffix string) bool {
	if !strings.HasSuffix(host, "."+suffix) || strings.Count(host, ".") != strings.Count(suffix, ".")+1 {
		return false
	}
	label := strings.TrimSuffix(host, "."+suffix)
	return publicHostLabelPattern.MatchString(label)
}

func hostWithoutPort(host string) string {
	if strings.Count(host, ":") == 0 {
		return strings.ToLower(host)
	}
	if parsed, err := url.Parse("https://" + host); err == nil && parsed.Hostname() != "" {
		return strings.ToLower(parsed.Hostname())
	}
	return strings.ToLower(host)
}

func formatOriginError(format string, args ...any) error {
	return fmt.Errorf("%w: %s", errOriginDenied, fmt.Sprintf(format, args...))
}
