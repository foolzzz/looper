package parity

import (
	"bytes"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
)

// Raw golden convention (go/testdata/parity/raw/):
//
// A file <name>.http is the exact byte string Python _json_response or
// _no_content returns: status line, headers in Python's insertion order,
// Content-Length of the unreplaced body, then one blank line and the body.
// There is no Date header. make_goldens.py --raw-only writes these files and
// does not rewrite the sorted JSON goldens.
//
// CompareRaw checks protocol version, status code, reason phrase, body bytes, and every header
// except Date. net/http writes Date itself and sorts header names on the
// wire, so names are compared as a map. Header values are not sorted:
// Set-Cookie order is significant. Cache-Control, WWW-Authenticate, Allow,
// Retry-After, Content-Type, and Content-Length are product headers.
// A header the response did not send is missing. Response.ContentLength
// is not copied into the header map, so a 204 that omits Content-Length
// does not match a golden that sends Content-Length: 0.
//
// An optional <name>.paths file lists JSON Pointers, one per line. Only those
// value spans are replaced with "<volatile>" (the quotes are part of the
// token) before the body compare. The splice does not sort keys, re-encode
// numbers, or delete fields. A missing path fails. Content-Length is checked
// against the body from before that splice, then compared across the two
// responses. A volatile value whose byte length differs still fails
// Content-Length; later cards must keep the length or extend this convention
// in this file. They must not drop the header.
//
// raw/PENDING.txt names the structured goldens that do not have a raw file
// yet. A goldenCases entry is either pending or raw/<name>.http, not both.
// /api/status and every non-200 golden are raw. no-content.http is the
// OPTIONS 204 shape and is not served. error-not-found.http,
// error-bad-host.http, and error-cross-origin.http are extra live responses.

// CompareRaw reports how resp differs from a Python response byte string.
// body is the response body already read. paths are JSON Pointers.
func CompareRaw(resp *http.Response, body, golden []byte, paths []string) error {
	if resp == nil {
		return fmt.Errorf("raw response: nil")
	}
	wantCode, wantStatus, wantHeader, wantBody, err := parsePythonResponse(golden)
	if err != nil {
		return err
	}
	if resp.Proto != "HTTP/1.1" || resp.ProtoMajor != 1 || resp.ProtoMinor != 1 {
		return fmt.Errorf("raw protocol %q (%d.%d), want HTTP/1.1", resp.Proto, resp.ProtoMajor, resp.ProtoMinor)
	}
	gotHeader := resp.Header.Clone()
	if gotHeader == nil {
		gotHeader = make(http.Header)
	}
	gotHeader.Del("Date")
	wantHeader = wantHeader.Clone()
	wantHeader.Del("Date")
	if err := checkContentLength(gotHeader, body); err != nil {
		return fmt.Errorf("raw response: %w", err)
	}
	if err := checkContentLength(wantHeader, wantBody); err != nil {
		return fmt.Errorf("raw golden: %w", err)
	}
	gotStatus := resp.Status
	if gotStatus == "" {
		gotStatus = strconv.Itoa(resp.StatusCode) + " " + http.StatusText(resp.StatusCode)
		gotStatus = strings.TrimSpace(gotStatus)
	}
	if resp.StatusCode != wantCode || gotStatus != wantStatus {
		return fmt.Errorf("raw status %s, want %s", gotStatus, wantStatus)
	}
	if err := sameHeaders(gotHeader, wantHeader); err != nil {
		return fmt.Errorf("raw headers: %w", err)
	}
	gotBody, err := replaceJSONPointers(body, paths)
	if err != nil {
		return fmt.Errorf("raw response body: %w", err)
	}
	wantReplaced, err := replaceJSONPointers(wantBody, paths)
	if err != nil {
		return fmt.Errorf("raw golden body: %w", err)
	}
	if !bytes.Equal(gotBody, wantReplaced) {
		return fmt.Errorf("raw body %s", clipDiff(gotBody, wantReplaced))
	}
	return nil
}

func checkContentLength(h http.Header, body []byte) error {
	values := h.Values("Content-Length")
	if len(values) != 1 {
		return fmt.Errorf("Content-Length %q", values)
	}
	if values[0] != strconv.Itoa(len(body)) {
		return fmt.Errorf("Content-Length %s, body is %d bytes", values[0], len(body))
	}
	return nil
}

func sameHeaders(got, want http.Header) error {
	for key, wantVals := range want {
		gotVals, ok := got[key]
		if !ok {
			return fmt.Errorf("missing %s", key)
		}
		if len(gotVals) != len(wantVals) {
			return fmt.Errorf("%s %q, want %q", key, gotVals, wantVals)
		}
		for i := range wantVals {
			if gotVals[i] != wantVals[i] {
				return fmt.Errorf("%s %q, want %q", key, gotVals, wantVals)
			}
		}
	}
	for key := range got {
		if _, ok := want[key]; !ok {
			return fmt.Errorf("extra %s %q", key, got.Values(key))
		}
	}
	return nil
}

func parsePythonResponse(raw []byte) (int, string, http.Header, []byte, error) {
	head, body, ok := bytes.Cut(raw, []byte("\r\n\r\n"))
	if !ok {
		return 0, "", nil, nil, fmt.Errorf("parse python response: no header break")
	}
	lines := bytes.Split(head, []byte("\r\n"))
	if len(lines) == 0 || len(lines[0]) == 0 {
		return 0, "", nil, nil, fmt.Errorf("parse python response: empty status")
	}
	statusLine := string(lines[0])
	const prefix = "HTTP/1.1 "
	if !strings.HasPrefix(statusLine, prefix) {
		return 0, "", nil, nil, fmt.Errorf("parse python response: %s", statusLine)
	}
	status := strings.TrimPrefix(statusLine, prefix)
	codeText, _, _ := strings.Cut(status, " ")
	code, err := strconv.Atoi(codeText)
	if err != nil {
		return 0, "", nil, nil, fmt.Errorf("parse python response: %s", statusLine)
	}
	header := make(http.Header)
	for _, line := range lines[1:] {
		if len(line) == 0 {
			return 0, "", nil, nil, fmt.Errorf("parse python response: empty header line")
		}
		key, value, found := bytes.Cut(line, []byte(":"))
		if !found {
			return 0, "", nil, nil, fmt.Errorf("parse python response: %s", line)
		}
		header.Add(string(bytes.TrimSpace(key)), string(bytes.TrimSpace(value)))
	}
	return code, status, header, body, nil
}

func clipDiff(got, want []byte) string {
	if len(got) > 180 || len(want) > 180 {
		return fmt.Sprintf("len %d vs %d\n got %q\nwant %q", len(got), len(want), clip(got), clip(want))
	}
	return fmt.Sprintf("\n got %s\nwant %s", got, want)
}

func clip(b []byte) []byte {
	if len(b) > 120 {
		return append(append([]byte{}, b[:120]...), []byte("...")...)
	}
	return b
}

// readPointerFile reads raw/<name>.paths. A missing file means no
// substitutions. Blank lines and lines starting with # are ignored.
func readPointerFile(path string) ([]string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var out []string
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimRight(line, "\r")
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		out = append(out, line)
	}
	return out, nil
}
