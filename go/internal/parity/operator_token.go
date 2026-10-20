package parity

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/httpapi"
	"github.com/foolzzz/looper/go/internal/operatorauth"
)

// useOperatorToken gives the parity server its own runtime directory and token.
// It does not change RuntimeRoot: knowledge checks keep the fixture project.
func useOperatorToken(t *testing.T, api *httpapi.Server) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	token, err := operatorauth.EnsureToken(ctx, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	api.OperatorAuth = true
	api.OperatorToken = token
	return token
}

type bearerRoundTripper struct {
	base  http.RoundTripper
	token string
}

func (t bearerRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	clone := req.Clone(req.Context())
	clone.Header.Set("Authorization", "Bearer "+t.token)
	base := t.base
	if base == nil {
		base = http.DefaultTransport
	}
	return base.RoundTrip(clone)
}

func useBearer(client *http.Client, token string) {
	client.Transport = bearerRoundTripper{base: client.Transport, token: token}
}
