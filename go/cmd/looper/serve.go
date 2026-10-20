package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/foolzzz/looper/go/internal/config"
	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/httpapi"
	"github.com/foolzzz/looper/go/internal/operatorauth"
)

const shutdownTimeout = 10 * time.Second

// schemaTimeout bounds EnsureSchema. The signal context has no deadline, and a
// stuck connection or advisory lock must not stall startup forever.
const schemaTimeout = 60 * time.Second

func serve(args []string, logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return serveContext(ctx, args, logger)
}

func serveContext(ctx context.Context, args []string, logger *slog.Logger) error {
	cfg, err := config.ParseServeArgs(args)
	if err != nil {
		return err
	}
	if err := checkWebDir(cfg.WebDir); err != nil {
		return err
	}
	// The runtime root and token are checked before any database dial.
	// An empty --runtime-root is the process directory, not ~/.looper.
	root := cfg.RuntimeRoot
	if root == "" {
		root = "."
	}
	token, err := operatorauth.EnsureToken(ctx, root)
	if err != nil {
		return err
	}
	sqlDB, err := db.Open(ctx, cfg.DBURL)
	if err != nil {
		return err
	}
	defer sqlDB.Close()

	// Loopback only. A later milestone can add Python's --host.
	ln, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(cfg.Port)))
	if err != nil {
		return fmt.Errorf("listen on 127.0.0.1:%d: %w", cfg.Port, err)
	}
	logger.Info("listening", "addr", ln.Addr().String(), "version", version)
	// After listen: a refused database must not stop the port from opening.
	// Schema SQL errors are logged without the connection string. Python's
	// data migrations are not run.
	schemaCtx, schemaCancel := context.WithTimeout(ctx, schemaTimeout)
	schemaErr := db.EnsureSchema(schemaCtx, cfg.DBURL)
	schemaCancel()
	if schemaErr != nil {
		logger.Error("ensure schema", "err", db.ErrorText(schemaErr))
	}
	// scheduler_settings.load runs after the schema exists. A stored minimum
	// wins over the deprecated variable, so a bad variable does not stop a
	// start that a saved setting allows. When the schema could not be read,
	// only the variable applies. A variable that is not a whole number of
	// seconds from 1 closes the listener instead of serving.
	loadCtx, loadCancel := context.WithTimeout(ctx, schemaTimeout)
	var intervalErr error
	if schemaErr != nil {
		intervalErr = db.ConfigureScheduleMinInterval()
	} else {
		intervalErr = db.LoadScheduleMinInterval(loadCtx, sqlDB)
	}
	loadCancel()
	if intervalErr != nil {
		_ = ln.Close()
		if ctx.Err() != nil {
			return nil
		}
		return intervalErr
	}
	api := httpapi.New(version, db.Probe{DB: sqlDB}, cfg.WebDir)
	api.Reads = db.NewStore(sqlDB)
	api.RuntimeRoot = root
	api.OperatorAuth = true
	api.OperatorToken = token
	api.Port = cfg.Port
	return serveListener(ctx, ln, api.Handler(), logger, shutdownTimeout)
}

func checkWebDir(dir string) error {
	if dir == "" {
		return nil
	}
	info, err := os.Stat(dir)
	if err != nil {
		return &config.UsageError{Err: fmt.Errorf("--web-dir %q: %w", dir, err)}
	}
	if !info.IsDir() {
		return &config.UsageError{Err: fmt.Errorf("--web-dir %q is not a directory", dir)}
	}
	return nil
}

// serveListener runs srv until ctx is cancelled, then shuts down within timeout.
// Request contexts are not tied to ctx, so Shutdown can finish in-flight work.
// Handler's BindShutdown closes hijacked chat sockets from Shutdown and Close.
func serveListener(ctx context.Context, ln net.Listener, handler http.Handler, logger *slog.Logger, timeout time.Duration) error {
	srv := &http.Server{
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		ErrorLog:          slog.NewLogLogger(logger.Handler(), slog.LevelWarn),
	}
	if binder, ok := handler.(httpapi.ShutdownBinder); ok {
		ln = binder.BindShutdown(srv, ln)
	}
	errCh := make(chan error, 1)
	go func() {
		err := srv.Serve(ln)
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		errCh <- err
	}()
	select {
	case err := <-errCh:
		if err != nil {
			return fmt.Errorf("http server: %w", err)
		}
		return nil
	case <-ctx.Done():
		shutCtx, cancel := context.WithTimeout(context.Background(), timeout)
		defer cancel()
		if err := srv.Shutdown(shutCtx); err != nil {
			// Shutdown left connections open. Close cancels their request contexts.
			_ = srv.Close()
			return fmt.Errorf("shutdown http server: %w", err)
		}
		return nil
	}
}
