package gateway

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strings"
)

const (
	defaultListenAddr    = ":8080"
	defaultRequestLimit  = 1 << 20
	fixedRegion          = "asia-northeast3"
	destinationClassPath = "/v1/apps/authorize"
)

var (
	errInvalidConfig = errors.New("gateway configuration is invalid")

	projectIDPattern          = regexp.MustCompile(`^[a-z][a-z0-9-]{4,28}[a-z0-9]$`)
	projectNumberPattern      = regexp.MustCompile(`^[1-9][0-9]{11}$`)
	cloudflareAudiencePattern = regexp.MustCompile(`^[A-Za-z0-9._-]{8,200}$`)
	keyIDPattern              = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)
	serviceAccountPattern     = regexp.MustCompile(`^[a-z][a-z0-9-]{4,28}[a-z0-9]$`)
	publicSuffixPattern       = regexp.MustCompile(`^[a-z0-9-]+\.[a-z0-9-]+$`)
)

type Config struct {
	ListenAddr                 string
	PublicSuffix               string
	ProjectID                  string
	ProjectNumber              string
	Region                     string
	AccessIssuer               string
	AccessAudience             string
	GatewayServiceAccountEmail string
	GatewayOrigin              string
	GatewayOriginHost          string
	AuthorizationURL           string
	AuthorizationAudience      string
	CurrentProofKeyID          string
	CurrentProofSecret         []byte
	PreviousProofKeyID         string
	PreviousProofSecret        []byte
	RequestBodyLimit           int64
	proofKeys                  map[string][]byte
}

func LoadConfigFromEnv() (Config, error) {
	return LoadConfigFromMap(loadEnviron(os.Environ()))
}

func LoadConfigFromMap(env map[string]string) (Config, error) {
	cfg := Config{
		ListenAddr:                 strings.TrimSpace(env["MIM_LISTEN_ADDR"]),
		PublicSuffix:               strings.ToLower(strings.TrimSpace(env["MIM_PUBLIC_SUFFIX"])),
		ProjectID:                  strings.TrimSpace(env["MIM_PROJECT_ID"]),
		ProjectNumber:              strings.TrimSpace(env["MIM_PROJECT_NUMBER"]),
		Region:                     strings.TrimSpace(env["MIM_REGION"]),
		AccessIssuer:               strings.TrimSpace(env["MIM_CLOUDFLARE_ACCESS_ISSUER"]),
		AccessAudience:             strings.TrimSpace(env["MIM_CLOUDFLARE_ACCESS_AUDIENCE"]),
		GatewayServiceAccountEmail: strings.TrimSpace(env["MIM_APP_GATEWAY_SERVICE_ACCOUNT_EMAIL"]),
		GatewayOrigin:              strings.TrimSpace(env["MIM_APP_GATEWAY_ORIGIN"]),
		AuthorizationURL:           strings.TrimSpace(env["MIM_APP_AUTHORIZATION_URL"]),
		AuthorizationAudience:      strings.TrimSpace(env["MIM_APP_AUTHORIZATION_AUDIENCE"]),
		CurrentProofKeyID:          strings.TrimSpace(env["MIM_APP_PROOF_CURRENT_KEY_ID"]),
		CurrentProofSecret:         []byte(env["MIM_APP_PROOF_CURRENT_SECRET"]),
		PreviousProofKeyID:         strings.TrimSpace(env["MIM_APP_PROOF_PREVIOUS_KEY_ID"]),
		PreviousProofSecret:        []byte(env["MIM_APP_PROOF_PREVIOUS_SECRET"]),
		RequestBodyLimit:           defaultRequestLimit,
	}
	if cfg.ListenAddr == "" {
		cfg.ListenAddr = defaultListenAddr
	}
	if err := cfg.validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func (cfg *Config) validate() error {
	if cfg.ListenAddr == "" {
		return fmt.Errorf("%w: missing listen address", errInvalidConfig)
	}
	if !publicSuffixPattern.MatchString(cfg.PublicSuffix) || strings.Count(cfg.PublicSuffix, ".") != 1 {
		return fmt.Errorf("%w: invalid public suffix", errInvalidConfig)
	}
	if !projectIDPattern.MatchString(cfg.ProjectID) {
		return fmt.Errorf("%w: invalid project id", errInvalidConfig)
	}
	if !projectNumberPattern.MatchString(cfg.ProjectNumber) {
		return fmt.Errorf("%w: invalid project number", errInvalidConfig)
	}
	if cfg.Region != fixedRegion {
		return fmt.Errorf("%w: invalid region", errInvalidConfig)
	}
	if issuer, ok := normalizeHTTPSOrigin(cfg.AccessIssuer); !ok || !strings.HasSuffix(issuer, ".cloudflareaccess.com") {
		return fmt.Errorf("%w: invalid access issuer", errInvalidConfig)
	} else {
		cfg.AccessIssuer = issuer
	}
	if !cloudflareAudiencePattern.MatchString(cfg.AccessAudience) {
		return fmt.Errorf("%w: invalid access audience", errInvalidConfig)
	}
	expectedGatewayEmail := "mim-app-gateway@" + cfg.ProjectID + ".iam.gserviceaccount.com"
	if !strings.EqualFold(cfg.GatewayServiceAccountEmail, expectedGatewayEmail) {
		return fmt.Errorf("%w: invalid gateway service account", errInvalidConfig)
	}
	cfg.GatewayServiceAccountEmail = expectedGatewayEmail
	expectedGatewayOrigin := fmt.Sprintf("https://mim-app-gateway-%s.%s.run.app", cfg.ProjectNumber, cfg.Region)
	if cfg.GatewayOrigin != expectedGatewayOrigin {
		return fmt.Errorf("%w: invalid gateway origin", errInvalidConfig)
	}
	cfg.GatewayOriginHost = strings.TrimPrefix(expectedGatewayOrigin, "https://")
	expectedAudience := fmt.Sprintf("https://mim-schedule-gateway-%s.%s.run.app", cfg.ProjectNumber, cfg.Region)
	if cfg.AuthorizationAudience != expectedAudience {
		return fmt.Errorf("%w: invalid authorization audience", errInvalidConfig)
	}
	if cfg.AuthorizationURL != expectedAudience+destinationClassPath {
		return fmt.Errorf("%w: invalid authorization url", errInvalidConfig)
	}
	if !keyIDPattern.MatchString(cfg.CurrentProofKeyID) || len(cfg.CurrentProofSecret) < 32 {
		return fmt.Errorf("%w: invalid current proof key", errInvalidConfig)
	}
	if (cfg.PreviousProofKeyID == "") != (len(cfg.PreviousProofSecret) == 0) {
		return fmt.Errorf("%w: partial previous key material", errInvalidConfig)
	}
	if cfg.PreviousProofKeyID != "" {
		if !keyIDPattern.MatchString(cfg.PreviousProofKeyID) || len(cfg.PreviousProofSecret) < 32 {
			return fmt.Errorf("%w: invalid previous proof key", errInvalidConfig)
		}
		if cfg.PreviousProofKeyID == cfg.CurrentProofKeyID {
			return fmt.Errorf("%w: duplicate proof key ids", errInvalidConfig)
		}
	}
	cfg.proofKeys = map[string][]byte{
		cfg.CurrentProofKeyID: append([]byte(nil), cfg.CurrentProofSecret...),
	}
	if cfg.PreviousProofKeyID != "" {
		cfg.proofKeys[cfg.PreviousProofKeyID] = append([]byte(nil), cfg.PreviousProofSecret...)
	}
	return nil
}

func loadEnviron(items []string) map[string]string {
	env := make(map[string]string, len(items))
	for _, item := range items {
		key, value, ok := strings.Cut(item, "=")
		if !ok {
			continue
		}
		env[key] = value
	}
	return env
}

func normalizeHTTPSOrigin(raw string) (string, bool) {
	parsed, err := url.Parse(raw)
	if err != nil {
		return "", false
	}
	if parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.Port() != "" || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.User != nil || parsed.Path != "" && parsed.Path != "/" {
		return "", false
	}
	if !serviceAccountPattern.MatchString(strings.Split(parsed.Hostname(), ".")[0]) && !strings.Contains(parsed.Hostname(), ".") {
		return "", false
	}
	return "https://" + strings.ToLower(parsed.Hostname()), true
}
