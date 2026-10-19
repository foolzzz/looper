package httpapi

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"

	"github.com/foolzzz/looper/go/internal/configtransfer"
	"github.com/foolzzz/looper/go/internal/db"
)

type configExporter interface {
	ExportConfig(context.Context, io.Writer, configtransfer.Options) (configtransfer.Summary, error)
}

func (s *Server) handleConfigExport(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/config/export" {
		notFound(w)
		return
	}
	exporter, ok := s.Reads.(configExporter)
	if !ok {
		requestFailed(w, "config export", errors.New("no database"))
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	// Context checks alone cannot interrupt a blocked socket Write or Flush.
	// Bound the download by the same deadline as its database snapshot.
	deadline, _ := ctx.Deadline()
	if err := http.NewResponseController(w).SetWriteDeadline(deadline); err != nil && !errors.Is(err, http.ErrNotSupported) {
		requestFailed(w, "config export write deadline", err)
		return
	}
	query := pythonQuery(r.URL.RawQuery)
	now := s.now()
	stream := &configExportWriter{ctx: ctx, response: w, filename: "looper-config-" + now.Format("2006-01-02") + ".json"}
	_, err := exporter.ExportConfig(ctx, stream, configtransfer.Options{
		IncludeProjects: queryValue(query, "projects") == "1",
		IncludeSecrets:  queryValue(query, "secrets") == "1",
		Now:             now,
		Version:         "2.10.0",
	})
	if err == nil && !stream.started {
		err = io.ErrUnexpectedEOF
	}
	if err != nil {
		if !stream.started {
			requestFailed(w, "config export", err)
			return
		}
		// Once the download starts, leave an incomplete JSON file rather than
		// appending an error object to a partially written configuration.
		slog.Warn("config export", "err", db.LogText(err))
	}
}

type configExportWriter struct {
	ctx      context.Context
	response http.ResponseWriter
	filename string
	started  bool
}

func (w *configExportWriter) Write(p []byte) (int, error) {
	if err := w.ctx.Err(); err != nil {
		return 0, err
	}
	if len(p) == 0 {
		return 0, nil
	}
	if !w.started {
		header := w.response.Header()
		header.Set("Content-Type", "application/json; charset=utf-8")
		header.Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", w.filename))
		header.Set("Cache-Control", "no-store")
		header.Set("Connection", "close")
		// Python sends only the four download headers, without an HTTP Date.
		header["Date"] = nil
		// net/http consumes identity without sending it: the HTTP/1.1 body
		// ends at connection EOF, matching Python rather than adding chunks.
		header.Set("Transfer-Encoding", "identity")
		w.response.WriteHeader(http.StatusOK)
		w.started = true
	}
	n, err := w.response.Write(p)
	if err != nil {
		return n, err
	}
	if n != len(p) {
		return n, io.ErrShortWrite
	}
	// Flush each piece so net/http cannot buffer the entire export and add
	// Content-Length. ResponseController also propagates a disconnected peer.
	if err := http.NewResponseController(w.response).Flush(); err != nil && !errors.Is(err, http.ErrNotSupported) {
		return n, err
	}
	return n, nil
}
