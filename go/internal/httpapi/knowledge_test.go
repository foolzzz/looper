package httpapi

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/foolzzz/looper/go/internal/db"
)

type knowledgeReads struct {
	*recordingReads
	body                       map[string]any
	err                        error
	project, item, root, route string
	ctx                        context.Context
}

func (r *recordingReads) Knowledge(context.Context, string, string) (map[string]any, error) {
	return nil, nil
}
func (r *recordingReads) Document(context.Context, string, string, string) (map[string]any, error) {
	return nil, nil
}
func (r *recordingReads) Drafts(context.Context, string) (map[string]any, error) { return nil, nil }
func (r *recordingReads) ProjectSkill(context.Context, string, string) (map[string]any, error) {
	return nil, nil
}

func (r *knowledgeReads) record(ctx context.Context, route, project, item, root string) (map[string]any, error) {
	r.ctx, r.route, r.project, r.item, r.root = ctx, route, project, item, root
	return r.body, r.err
}
func (r *knowledgeReads) Knowledge(ctx context.Context, project, root string) (map[string]any, error) {
	return r.record(ctx, "knowledge", project, "", root)
}
func (r *knowledgeReads) Document(ctx context.Context, project, id, root string) (map[string]any, error) {
	return r.record(ctx, "document", project, id, root)
}
func (r *knowledgeReads) Drafts(ctx context.Context, project string) (map[string]any, error) {
	return r.record(ctx, "drafts", project, "", "")
}
func (r *knowledgeReads) ProjectSkill(ctx context.Context, project, id string) (map[string]any, error) {
	return r.record(ctx, "skill", project, id, "")
}

func TestKnowledgeRoutes(t *testing.T) {
	const base = "/api/projects/proj-alpha0001/"
	cases := []struct {
		path, route, item string
		err               error
		status            int
		want              string
	}{
		{"knowledge", "knowledge", "", nil, 200, `"marker":"view"`},
		{"knowledge/doc-one", "document", "doc-one", nil, 200, `"marker":"view"`},
		{"knowledge/drafts", "drafts", "", nil, 200, `"marker":"view"`},
		{"skills/skill-one", "skill", "skill-one", nil, 200, `"marker":"view"`},
		{"knowledge", "knowledge", "", db.ErrProjectNotFound, 404, `project 'proj-alpha0001' not found`},
		{"knowledge/drafts", "drafts", "", db.ErrProjectNotFound, 404, `project 'proj-alpha0001' not found`},
		{"knowledge/doc-one", "document", "doc-one", db.ErrDocumentNotFound, 404, `has no document 'doc-one'`},
		{"skills/skill-one", "skill", "skill-one", db.ErrSkillNotFound, 404, `has no skill 'skill-one'`},
		{"knowledge", "knowledge", "", errors.New("SECRET"), 500, `the knowledge request failed; see the server log`},
		{"skills/skill-one", "skill", "skill-one", errors.New("SECRET"), 500, `the catalog request failed; see the server log`},
	}
	for _, tc := range cases {
		t.Run(tc.path+tc.want, func(t *testing.T) {
			reads := &knowledgeReads{recordingReads: &recordingReads{}, body: map[string]any{"marker": "view"}, err: tc.err}
			api := New("test", nil, "")
			api.Reads, api.RuntimeRoot = reads, t.TempDir()
			rr := httptest.NewRecorder()
			api.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, base+tc.path))
			if rr.Code != tc.status || !strings.Contains(rr.Body.String(), tc.want) || strings.Contains(rr.Body.String(), "SECRET") {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if reads.route != tc.route || reads.project != "proj-alpha0001" || reads.item != tc.item {
				t.Fatalf("read: %+v", reads)
			}
			if (tc.route == "knowledge" || tc.route == "document") && reads.root != api.RuntimeRoot {
				t.Fatalf("runtime root %q", reads.root)
			}
			if _, ok := reads.ctx.Deadline(); !ok {
				t.Fatal("read missing context deadline")
			}
		})
	}
	for _, path := range []string{"knowledge", "knowledge/drafts", "knowledge/doc-one", "skills/skill-one"} {
		for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodHead} {
			t.Run(method+path, func(t *testing.T) {
				reads := &knowledgeReads{recordingReads: &recordingReads{}}
				api := New("test", nil, "")
				api.Reads, api.RuntimeRoot = reads, t.TempDir()
				rr := httptest.NewRecorder()
				api.Handler().ServeHTTP(rr, loopbackRequest(method, base+path))
				if rr.Code != 404 || reads.route != "" {
					t.Fatalf("status %d route %q", rr.Code, reads.route)
				}
			})
		}
	}
	for _, path := range []string{"knowledge/doc-%6fne", "knowledge/%64rafts", "skills/skill-%6fne", "knowledge/nope", "knowledge/", "skills/skill-one/extra"} {
		rr := httptest.NewRecorder()
		New("test", nil, "").Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, base+path))
		if rr.Code != 404 {
			t.Fatalf("%s status %d", path, rr.Code)
		}
	}
}

func TestKnowledgeUnavailable(t *testing.T) {
	for _, path := range []string{"knowledge", "knowledge/drafts", "knowledge/doc-one", "skills/skill-one"} {
		rr := httptest.NewRecorder()
		New("test", nil, "").Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-one/"+path))
		if rr.Code != 500 {
			t.Fatalf("%s status %d", path, rr.Code)
		}
	}
	api := New("test", nil, "")
	api.Reads = &knowledgeReads{recordingReads: &recordingReads{}}
	rr := httptest.NewRecorder()
	api.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-one/knowledge"))
	if rr.Code != 500 || !strings.Contains(rr.Body.String(), "the knowledge request failed") {
		t.Fatalf("no runtime root: %d %s", rr.Code, rr.Body.String())
	}
}
