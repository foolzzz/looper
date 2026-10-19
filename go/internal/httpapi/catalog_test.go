package httpapi

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
)

func (r *recordingReads) Catalog(context.Context) (map[string]any, error) { return nil, nil }
func (r *recordingReads) CatalogItem(context.Context, string) (map[string]any, error) {
	return nil, nil
}
func (r *recordingReads) ProjectInstalls(context.Context, string) (map[string]any, error) {
	return nil, nil
}

type catalogReads struct {
	*recordingReads
	ctx    context.Context
	id     string
	err    error
	onRead func(context.Context)
}

func (r *catalogReads) Catalog(ctx context.Context) (map[string]any, error) {
	r.ctx = ctx
	if r.onRead != nil {
		r.onRead(ctx)
	}
	return map[string]any{"marker": "catalog"}, r.err
}
func (r *catalogReads) CatalogItem(ctx context.Context, id string) (map[string]any, error) {
	r.id = id
	return r.Catalog(ctx)
}
func (r *catalogReads) ProjectInstalls(ctx context.Context, id string) (map[string]any, error) {
	r.id = id
	return r.Catalog(ctx)
}

func TestCatalogRoutes(t *testing.T) {
	for _, tc := range []struct {
		path, id string
		err      error
		status   int
		want     string
	}{
		{"/api/catalog", "", nil, 200, `"marker":"catalog"`},
		{"/api/catalog/lib-one", "lib-one", nil, 200, `"marker":"catalog"`},
		{"/api/projects/proj-one/installs", "proj-one", nil, 200, `"marker":"catalog"`},
		{"/api/catalog/lib-one", "lib-one", db.ErrCatalogItemNotFound, 404, "no catalog item 'lib-one'"},
		{"/api/projects/proj-one/installs", "proj-one", db.ErrProjectNotFound, 404, "project 'proj-one' not found"},
		{"/api/catalog", "", errors.New("SECRET"), 500, "the catalog request failed; see the server log"},
		{"/api/catalog/lib-one", "lib-one", errors.New("SECRET"), 500, "the catalog request failed; see the server log"},
		{"/api/projects/proj-one/installs", "proj-one", errors.New("SECRET"), 500, "the catalog request failed; see the server log"},
	} {
		t.Run(tc.path+tc.want, func(t *testing.T) {
			reads := &catalogReads{recordingReads: &recordingReads{}, err: tc.err}
			api := New("test", nil, "")
			api.Reads = reads
			rr := httptest.NewRecorder()
			api.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, tc.path))
			if rr.Code != tc.status || !strings.Contains(rr.Body.String(), tc.want) || strings.Contains(rr.Body.String(), "SECRET") {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if reads.id != tc.id || reads.ctx == nil {
				t.Fatalf("read id %q context %v", reads.id, reads.ctx)
			}
			if _, ok := reads.ctx.Deadline(); !ok {
				t.Fatal("read missing context deadline")
			}
		})
	}
}

func TestCatalogRequestContext(t *testing.T) {
	for _, path := range []string{"/api/catalog", "/api/catalog/lib-one", "/api/projects/proj-one/installs"} {
		for _, earlierDeadline := range []bool{false, true} {
			name := "cancellation"
			if earlierDeadline {
				name = "earlier deadline and cancellation"
			}
			t.Run(path+"/"+name, func(t *testing.T) {
				parent := context.Background()
				deadline := time.Now().Add(readTimeout / 2)
				if earlierDeadline {
					var cancel context.CancelFunc
					parent, cancel = context.WithDeadline(parent, deadline)
					defer cancel()
				}
				parent, cancelParent := context.WithCancel(parent)
				defer cancelParent()
				reads := &catalogReads{recordingReads: &recordingReads{}}
				reads.onRead = func(ctx context.Context) {
					if earlierDeadline {
						if got, ok := ctx.Deadline(); !ok || !got.Equal(deadline) {
							t.Errorf("read deadline %v (present %t), want request deadline %v", got, ok, deadline)
						}
					}
					// Cancel while the read is in progress: the handler's deferred
					// cancel must not make a detached context appear to propagate.
					cancelParent()
					select {
					case <-ctx.Done():
						if !errors.Is(ctx.Err(), context.Canceled) {
							t.Errorf("read context error %v, want context.Canceled", ctx.Err())
						}
					case <-time.After(time.Second):
						t.Error("request cancellation did not reach the active read")
					}
				}
				api := New("test", nil, "")
				api.Reads = reads
				rr := httptest.NewRecorder()
				api.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, path).WithContext(parent))
				if reads.ctx == nil {
					t.Fatal("route did not invoke the database read")
				}
			})
		}
	}
}

func TestCatalogRouteBoundaries(t *testing.T) {
	for _, path := range []string{"/api/catalog", "/api/catalog/lib-one", "/api/projects/proj-one/installs"} {
		for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodDelete, http.MethodHead} {
			t.Run(method+path, func(t *testing.T) {
				reads := &catalogReads{recordingReads: &recordingReads{}}
				api := New("test", nil, "")
				api.Reads = reads
				rr := httptest.NewRecorder()
				api.Handler().ServeHTTP(rr, loopbackRequest(method, path))
				if rr.Code != 404 || reads.ctx != nil {
					t.Fatalf("status %d read context %v", rr.Code, reads.ctx)
				}
			})
		}
		rr := httptest.NewRecorder()
		New("test", nil, "").Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
		if rr.Code != 500 || !strings.Contains(rr.Body.String(), "the catalog request failed") {
			t.Fatalf("nil store: %d %s", rr.Code, rr.Body.String())
		}
	}
	for _, path := range []string{"/api/catalog/", "/api/catalog/Lib-one", "/api/catalog/lib-one-two", "/api/catalog/lib-%6fne", "/api/projects/proj-%6fne/installs", "/api/projects/one/installs", "/api/projects/proj-one/installs/"} {
		rr := httptest.NewRecorder()
		New("test", nil, "").Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
		if rr.Code != 404 {
			t.Fatalf("%s status %d", path, rr.Code)
		}
	}
}
