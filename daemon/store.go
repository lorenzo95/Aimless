package main

import (
	"database/sql"
	"encoding/base64"
	"sync"
)

// journalEntry is the in-memory shape shared by the outbox and inbox. Payload
// stays base64 here because the daemon API and Mail pass it around as text;
// SQLite stores the raw bytes.
type journalEntry struct {
	Seq     uint64       `json:"seq"`
	Ts      int64        `json:"ts"`
	Payload string       `json:"payload"`
	Type    EnvelopeType `json:"type,omitempty"`
	SentAt  int64        `json:"sent_at,omitempty"` // transient pacing state, not persisted
}

// OutboxJournal is the durable per-peer send queue. A row exists until the peer
// acknowledges its seq; delivery is at-least-once and idempotent at the peer.
type OutboxJournal struct {
	db     *DB
	peer   string
	sentMu sync.Mutex
	sent   map[uint64]int64 // transient "last enqueued" times, not persisted
}

func NewOutboxJournal(db *DB, peerHex string) (*OutboxJournal, error) {
	return &OutboxJournal{db: db, peer: peerHex, sent: make(map[uint64]int64)}, nil
}

// NextSeq allocates the next monotonic seq for this peer. The counter lives in
// its own table so acked (deleted) rows can never cause seq reuse.
func (j *OutboxJournal) NextSeq() (uint64, error) {
	tx, err := j.db.sql.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	var next int64
	err = tx.QueryRow(`SELECT next FROM outbox_seq WHERE peer = ?`, j.peer).Scan(&next)
	if err == sql.ErrNoRows {
		next = 1
		if _, err := tx.Exec(`INSERT INTO outbox_seq(peer, next) VALUES(?, 1)`, j.peer); err != nil {
			return 0, err
		}
	} else if err != nil {
		return 0, err
	}
	if _, err := tx.Exec(`UPDATE outbox_seq SET next = next + 1 WHERE peer = ?`, j.peer); err != nil {
		return 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return uint64(next), nil
}

func (j *OutboxJournal) Queue(seq uint64, ts int64, payload []byte) error {
	return j.QueueAs(seq, ts, payload, TypeMsg)
}

func (j *OutboxJournal) QueueAs(seq uint64, ts int64, payload []byte, typ EnvelopeType) error {
	_, err := j.db.sql.Exec(
		`INSERT OR REPLACE INTO outbox(peer, seq, ts, type, payload) VALUES(?, ?, ?, ?, ?)`,
		j.peer, int64(seq), ts, int64(typ), payload,
	)
	return err
}

// Pending returns every unacked entry in seq order, with the transient SentAt
// pacing stamp overlaid so the retry loop does not re-flood in-flight chunks.
func (j *OutboxJournal) Pending() []journalEntry {
	rows, err := j.db.sql.Query(
		`SELECT seq, ts, type, payload FROM outbox WHERE peer = ? ORDER BY seq`, j.peer)
	if err != nil {
		return nil
	}
	defer rows.Close()
	j.sentMu.Lock()
	defer j.sentMu.Unlock()
	var out []journalEntry
	for rows.Next() {
		var (
			seq, typ int64
			ts       int64
			payload  []byte
		)
		if err := rows.Scan(&seq, &ts, &typ, &payload); err != nil {
			continue
		}
		out = append(out, journalEntry{
			Seq: uint64(seq), Ts: ts, Type: EnvelopeType(typ),
			Payload: base64.StdEncoding.EncodeToString(payload), SentAt: j.sent[uint64(seq)],
		})
	}
	return out
}

// MarkSent records the in-memory send time of an entry so the retry loop does
// not re-send in-flight chunks. Transient: on restart everything is considered
// stale and re-sent, which is correct for durability.
func (j *OutboxJournal) MarkSent(seq uint64, at int64) {
	j.sentMu.Lock()
	j.sent[seq] = at
	j.sentMu.Unlock()
}

// Ack removes an entry. Returns true if the seq was still pending.
func (j *OutboxJournal) Ack(seq uint64) (bool, error) {
	res, err := j.db.sql.Exec(`DELETE FROM outbox WHERE peer = ? AND seq = ?`, j.peer, int64(seq))
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	j.sentMu.Lock()
	delete(j.sent, seq)
	j.sentMu.Unlock()
	return n > 0, nil
}

// InboxStore is the durable per-peer receive log. It retains the newest
// `capacity` messages and remembers trimmed seqs as a bounded replay guard.
type InboxStore struct {
	db       *DB
	peer     string
	capacity int
	seenCap  int
}

func NewInboxStore(db *DB, peerHex string, capacity int) (*InboxStore, error) {
	if capacity < 1 {
		capacity = 1
	}
	return &InboxStore{db: db, peer: peerHex, capacity: capacity, seenCap: 4 * capacity}, nil
}

// Add stores one message. It returns isNew=false for a duplicate seq (retained
// or trimmed-and-remembered).
func (in *InboxStore) Add(seq uint64, ts int64, payload []byte) (bool, error) {
	tx, err := in.db.sql.Begin()
	if err != nil {
		return false, err
	}
	defer tx.Rollback()

	var one int
	switch err := tx.QueryRow(`SELECT 1 FROM inbox WHERE peer = ? AND seq = ?`, in.peer, int64(seq)).Scan(&one); err {
	case nil:
		return false, nil
	case sql.ErrNoRows:
	default:
		return false, err
	}
	switch err := tx.QueryRow(`SELECT 1 FROM inbox_seen WHERE peer = ? AND seq = ?`, in.peer, int64(seq)).Scan(&one); err {
	case nil:
		return false, nil
	case sql.ErrNoRows:
	default:
		return false, err
	}

	if _, err := tx.Exec(
		`INSERT INTO inbox(peer, seq, ts, payload) VALUES(?, ?, ?, ?)`,
		in.peer, int64(seq), ts, payload,
	); err != nil {
		return false, err
	}
	if err := in.trimTx(tx); err != nil {
		return false, err
	}
	if err := tx.Commit(); err != nil {
		return true, err
	}
	return true, nil
}

// trimTx evicts oldest entries past capacity into the bounded replay guard.
func (in *InboxStore) trimTx(tx *sql.Tx) error {
	var count int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM inbox WHERE peer = ?`, in.peer).Scan(&count); err != nil {
		return err
	}
	for count > in.capacity {
		var oldest int64
		if err := tx.QueryRow(`SELECT MIN(seq) FROM inbox WHERE peer = ?`, in.peer).Scan(&oldest); err != nil {
			return err
		}
		if _, err := tx.Exec(`INSERT OR IGNORE INTO inbox_seen(peer, seq) VALUES(?, ?)`, in.peer, oldest); err != nil {
			return err
		}
		if _, err := tx.Exec(`DELETE FROM inbox WHERE peer = ? AND seq = ?`, in.peer, oldest); err != nil {
			return err
		}
		count--
	}
	// Bound the replay guard to seenCap newest seqs.
	if _, err := tx.Exec(
		`DELETE FROM inbox_seen WHERE peer = ? AND seq NOT IN (
		     SELECT seq FROM inbox_seen WHERE peer = ? ORDER BY seq DESC LIMIT ?
		 )`, in.peer, in.peer, in.seenCap); err != nil {
		return err
	}
	var min sql.NullInt64
	if err := tx.QueryRow(`SELECT MIN(seq) FROM inbox WHERE peer = ?`, in.peer).Scan(&min); err != nil {
		return err
	}
	retained := int64(0)
	if min.Valid {
		retained = min.Int64
	}
	_, err := tx.Exec(
		`INSERT INTO peer_state(peer, retained_min) VALUES(?, ?)
		 ON CONFLICT(peer) DO UPDATE SET retained_min = excluded.retained_min`,
		in.peer, retained)
	return err
}

// After returns entries with seq strictly greater than afterSeq, in order.
func (in *InboxStore) After(afterSeq uint64) []journalEntry {
	rows, err := in.db.sql.Query(
		`SELECT seq, ts, payload FROM inbox WHERE peer = ? AND seq > ? ORDER BY seq`,
		in.peer, int64(afterSeq))
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []journalEntry
	for rows.Next() {
		var (
			seq, ts int64
			payload []byte
		)
		if err := rows.Scan(&seq, &ts, &payload); err != nil {
			continue
		}
		out = append(out, journalEntry{Seq: uint64(seq), Ts: ts, Payload: base64.StdEncoding.EncodeToString(payload)})
	}
	return out
}

func (in *InboxStore) Oldest() uint64 {
	var v sql.NullInt64
	if err := in.db.sql.QueryRow(`SELECT MIN(seq) FROM inbox WHERE peer = ?`, in.peer).Scan(&v); err != nil || !v.Valid {
		return 0
	}
	return uint64(v.Int64)
}

func (in *InboxStore) Latest() uint64 {
	var v sql.NullInt64
	if err := in.db.sql.QueryRow(`SELECT MAX(seq) FROM inbox WHERE peer = ?`, in.peer).Scan(&v); err != nil || !v.Valid {
		return 0
	}
	return uint64(v.Int64)
}
