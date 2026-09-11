package main

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	_ "modernc.org/sqlite"
)

// DB is the single SQLite connection for a datadir. modernc.org/sqlite is a
// pure-Go driver, so the daemon stays a static CGO_ENABLED=0 binary.
//
// MaxOpenConns(1) plus the write mutex serializes access. The daemon's traffic
// is low, and this makes "database is locked" impossible without surrendering
// the transactional guarantees the hand-rolled append-logs lacked.
type DB struct {
	sql *sql.DB
	mu  sync.Mutex
}

const schemaVersion = 1

const schemaSQL = `
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    peer    TEXT    NOT NULL,
    seq     INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    type    INTEGER NOT NULL DEFAULT 1,
    payload BLOB    NOT NULL,
    PRIMARY KEY (peer, seq)
);

CREATE TABLE IF NOT EXISTS outbox_seq (
    peer TEXT    PRIMARY KEY,
    next INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox (
    peer    TEXT    NOT NULL,
    seq     INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    payload BLOB    NOT NULL,
    PRIMARY KEY (peer, seq)
);

-- seqs that were accepted but trimmed out of the retained window, kept as a
-- bounded replay guard.
CREATE TABLE IF NOT EXISTS inbox_seen (
    peer TEXT    NOT NULL,
    seq  INTEGER NOT NULL,
    PRIMARY KEY (peer, seq)
);

CREATE TABLE IF NOT EXISTS peer_state (
    peer         TEXT    PRIMARY KEY,
    retained_min INTEGER NOT NULL DEFAULT 0,
    ack_upto     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS attach (
    peer    TEXT    NOT NULL,
    tid     TEXT    NOT NULL,
    idx     INTEGER NOT NULL,
    total   INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    payload BLOB    NOT NULL,
    PRIMARY KEY (peer, tid, idx)
);
`

func OpenDB(datadir string) (*DB, error) {
	if err := os.MkdirAll(datadir, 0o700); err != nil {
		return nil, fmt.Errorf("create datadir: %w", err)
	}
	path := filepath.Join(datadir, "aimless.db")
	dsn := "file:" + path +
		"?_pragma=busy_timeout(5000)" +
		"&_pragma=journal_mode(WAL)" +
		"&_pragma=synchronous(NORMAL)"
	sdb, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open db: %w", err)
	}
	sdb.SetMaxOpenConns(1)
	db := &DB{sql: sdb}
	if _, err := db.sql.Exec(schemaSQL); err != nil {
		sdb.Close()
		return nil, fmt.Errorf("apply schema: %w", err)
	}
	if _, err := db.sql.Exec(
		`INSERT INTO meta(key, value) VALUES('schema', ?) ON CONFLICT(key) DO NOTHING`,
		fmt.Sprint(schemaVersion),
	); err != nil {
		sdb.Close()
		return nil, fmt.Errorf("stamp schema: %w", err)
	}
	return db, nil
}

func (db *DB) Close() error { return db.sql.Close() }
