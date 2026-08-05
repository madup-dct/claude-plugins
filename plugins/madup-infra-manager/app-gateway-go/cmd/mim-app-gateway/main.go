package main

import (
	"log"
	"net/http"
	"time"

	"mim-app-gateway/gateway"
)

func main() {
	cfg, err := gateway.LoadConfigFromEnv()
	if err != nil {
		log.Fatalf("load config: %v", err)
	}
	controlClient := gateway.NewControlHTTPClient()
	metadataSource := gateway.NewMetadataIDTokenSource(controlClient)
	accessVerifier := gateway.NewAccessTokenVerifier(gateway.AccessTokenVerifierConfig{
		Issuer:   cfg.AccessIssuer,
		Audience: cfg.AccessAudience,
		JWKSURL:  cfg.AccessIssuer + "/cdn-cgi/access/certs",
		Client:   controlClient,
		Now:      time.Now,
		CacheTTL: 5 * time.Minute,
	})
	authorizer, err := gateway.NewAuthorizationClient(gateway.AuthorizationClientConfig{
		URL:         cfg.AuthorizationURL,
		Audience:    cfg.AuthorizationAudience,
		TokenSource: metadataSource,
		Client:      controlClient,
	})
	if err != nil {
		log.Fatalf("build authorizer: %v", err)
	}
	handler, err := gateway.NewServer(cfg, gateway.ServerDeps{
		Clock:               time.Now,
		AccessVerifier:      accessVerifier,
		Authorizer:          authorizer,
		UpstreamTokenSource: metadataSource,
		Transport:           http.DefaultTransport,
	})
	if err != nil {
		log.Fatalf("build server: %v", err)
	}
	server := gateway.NewHTTPServer(cfg, handler)
	log.Fatal(server.ListenAndServe())
}
