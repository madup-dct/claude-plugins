package gateway

import (
	"strings"
	"testing"
)

func TestLoadConfigAcceptsStrictReviewedValues(t *testing.T) {
	env := validConfigEnv()

	cfg, err := LoadConfigFromMap(env)
	if err != nil {
		t.Fatalf("LoadConfigFromMap() error = %v", err)
	}

	if cfg.PublicSuffix != "madup.app" {
		t.Fatalf("PublicSuffix = %q, want madup.app", cfg.PublicSuffix)
	}
	if cfg.ProjectID != "mim-prod-123456" {
		t.Fatalf("ProjectID = %q", cfg.ProjectID)
	}
	if cfg.ProjectNumber != "123456789012" {
		t.Fatalf("ProjectNumber = %q", cfg.ProjectNumber)
	}
	if cfg.Region != "asia-northeast3" {
		t.Fatalf("Region = %q", cfg.Region)
	}
	if cfg.AuthorizationAudience != "https://mim-schedule-gateway-123456789012.asia-northeast3.run.app" {
		t.Fatalf("AuthorizationAudience = %q", cfg.AuthorizationAudience)
	}
	if cfg.GatewayOrigin != "https://mim-app-gateway-123456789012.asia-northeast3.run.app" {
		t.Fatalf("GatewayOrigin = %q", cfg.GatewayOrigin)
	}
	if cfg.CurrentProofKeyID != "app-current" {
		t.Fatalf("CurrentProofKeyID = %q", cfg.CurrentProofKeyID)
	}
	if cfg.PreviousProofKeyID != "app-previous" {
		t.Fatalf("PreviousProofKeyID = %q", cfg.PreviousProofKeyID)
	}
	if cfg.ListenAddr != ":8080" {
		t.Fatalf("ListenAddr = %q", cfg.ListenAddr)
	}
}

func TestLoadConfigRejectsInvalidMaterial(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name   string
		mutate func(map[string]string)
	}{
		{
			name: "missing suffix",
			mutate: func(env map[string]string) {
				delete(env, "MIM_PUBLIC_SUFFIX")
			},
		},
		{
			name: "bad suffix",
			mutate: func(env map[string]string) {
				env["MIM_PUBLIC_SUFFIX"] = "nested.madup.app"
			},
		},
		{
			name: "non seoul region",
			mutate: func(env map[string]string) {
				env["MIM_REGION"] = "us-central1"
			},
		},
		{
			name: "cross project gateway origin",
			mutate: func(env map[string]string) {
				env["MIM_APP_GATEWAY_ORIGIN"] = "https://mim-app-gateway-999999999999.asia-northeast3.run.app"
			},
		},
		{
			name: "cross project auth audience",
			mutate: func(env map[string]string) {
				env["MIM_APP_AUTHORIZATION_AUDIENCE"] = "https://mim-schedule-gateway-999999999999.asia-northeast3.run.app"
			},
		},
		{
			name: "cross project auth url",
			mutate: func(env map[string]string) {
				env["MIM_APP_AUTHORIZATION_URL"] = "https://mim-schedule-gateway-999999999999.asia-northeast3.run.app/v1/apps/authorize"
			},
		},
		{
			name: "short current key",
			mutate: func(env map[string]string) {
				env["MIM_APP_PROOF_CURRENT_SECRET"] = strings.Repeat("a", 31)
			},
		},
		{
			name: "duplicate key id",
			mutate: func(env map[string]string) {
				env["MIM_APP_PROOF_PREVIOUS_KEY_ID"] = env["MIM_APP_PROOF_CURRENT_KEY_ID"]
			},
		},
		{
			name: "partial previous key material",
			mutate: func(env map[string]string) {
				delete(env, "MIM_APP_PROOF_PREVIOUS_SECRET")
			},
		},
		{
			name: "gateway identity from another project",
			mutate: func(env map[string]string) {
				env["MIM_APP_GATEWAY_SERVICE_ACCOUNT_EMAIL"] = "mim-app-gateway@other-project.iam.gserviceaccount.com"
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			env := validConfigEnv()
			tc.mutate(env)
			if _, err := LoadConfigFromMap(env); err == nil {
				t.Fatalf("LoadConfigFromMap() succeeded for %s", tc.name)
			}
		})
	}
}

func validConfigEnv() map[string]string {
	return map[string]string{
		"MIM_LISTEN_ADDR":                       ":8080",
		"MIM_PUBLIC_SUFFIX":                     "madup.app",
		"MIM_PROJECT_ID":                        "mim-prod-123456",
		"MIM_PROJECT_NUMBER":                    "123456789012",
		"MIM_REGION":                            "asia-northeast3",
		"MIM_CLOUDFLARE_ACCESS_ISSUER":          "https://madup.cloudflareaccess.com",
		"MIM_CLOUDFLARE_ACCESS_AUDIENCE":        "cf-aud-app-1234567890",
		"MIM_APP_GATEWAY_SERVICE_ACCOUNT_EMAIL": "mim-app-gateway@mim-prod-123456.iam.gserviceaccount.com",
		"MIM_APP_GATEWAY_ORIGIN":                "https://mim-app-gateway-123456789012.asia-northeast3.run.app",
		"MIM_APP_AUTHORIZATION_URL":             "https://mim-schedule-gateway-123456789012.asia-northeast3.run.app/v1/apps/authorize",
		"MIM_APP_AUTHORIZATION_AUDIENCE":        "https://mim-schedule-gateway-123456789012.asia-northeast3.run.app",
		"MIM_APP_PROOF_CURRENT_KEY_ID":          "app-current",
		"MIM_APP_PROOF_CURRENT_SECRET":          strings.Repeat("c", 32),
		"MIM_APP_PROOF_PREVIOUS_KEY_ID":         "app-previous",
		"MIM_APP_PROOF_PREVIOUS_SECRET":         strings.Repeat("p", 32),
	}
}
