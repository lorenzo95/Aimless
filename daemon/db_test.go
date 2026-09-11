package main

import "testing"

// testDB opens a fresh SQLite store for a test datadir, closed automatically.
func testDB(t *testing.T, dir string) *DB {
	t.Helper()
	db, err := OpenDB(dir)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}
