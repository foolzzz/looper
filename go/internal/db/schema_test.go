package db

import (
	"os"
	"testing"
)

func TestEmbeddedSchemaMatchesSource(t *testing.T) {
	path, err := FindSchema()
	if err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != schemaSQL {
		t.Fatal("go/internal/db/schema.sql differs from looper/db/schema.sql; run go generate ./internal/db")
	}
}
