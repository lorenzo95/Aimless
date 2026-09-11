package main

import (
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
)

// TypeFile chunk wire payload layout: a 20-byte unencrypted routing header
// (transfer id + chunk index/total) followed by the E2E-sealed chunk body. The
// daemon reads only the header — enough to key the store and judge
// completeness — while filename/hash/data stay encrypted end to end.
const attachHeaderSize = 20

func parseFileHeader(payload []byte) (tid string, index, total uint16, ok bool) {
	if len(payload) < attachHeaderSize {
		return "", 0, 0, false
	}
	index = binary.LittleEndian.Uint16(payload[16:18])
	total = binary.LittleEndian.Uint16(payload[18:20])
	if total == 0 || index >= total {
		return "", 0, 0, false
	}
	tid = hex.EncodeToString(payload[:16])
	return tid, index, total, true
}

type attachEntry struct {
	Tid     string `json:"tid"`
	Seq     uint64 `json:"seq,omitempty"`
	Index   uint16 `json:"index"`
	Total   uint16 `json:"total"`
	Ts      int64  `json:"ts"`
	Payload string `json:"payload"`
}

// AttachPending describes a complete-but-unconsumed transfer.
type AttachPending struct {
	Tid   string `json:"tid"`
	Total uint16 `json:"total"`
	Ts    int64  `json:"ts"`
}

// AttachmentStore is the per-peer landing buffer for incoming TypeFile chunks.
// A transfer is retained until the client acknowledges consumption (AckTid), so
// files are as durable as text. A per-peer byte budget bounds disk use;
// eviction drops the oldest incomplete transfer wholesale first, then the
// oldest complete-but-unconsumed one.
type AttachmentStore struct {
	db       *DB
	peer     string
	capBytes int64
}

func NewAttachmentStore(db *DB, peerHex string, capBytes int64) (*AttachmentStore, error) {
	return &AttachmentStore{db: db, peer: peerHex, capBytes: capBytes}, nil
}

// Add stores one chunk (dedup by tid+index) after enforcing the byte budget.
// Returns isNew=false for a duplicate chunk.
func (as *AttachmentStore) Add(tid string, index, total uint16, seq uint64, ts int64, payload []byte) (bool, error) {
	tx, err := as.db.sql.Begin()
	if err != nil {
		return false, err
	}
	defer tx.Rollback()

	var one int
	switch err := tx.QueryRow(
		`SELECT 1 FROM attach WHERE peer = ? AND tid = ? AND idx = ?`,
		as.peer, tid, int(index)).Scan(&one); err {
	case nil:
		return false, nil
	case sql.ErrNoRows:
	default:
		return false, err
	}

	if err := as.makeRoomTx(tx, int64(len(payload)), tid); err != nil {
		return false, err
	}
	if _, err := tx.Exec(
		`INSERT INTO attach(peer, tid, idx, total, seq, ts, payload) VALUES(?, ?, ?, ?, ?, ?, ?)`,
		as.peer, tid, int(index), int(total), int64(seq), ts, payload,
	); err != nil {
		return false, err
	}
	if err := tx.Commit(); err != nil {
		return true, err
	}
	return true, nil
}

type attachTransferRow struct {
	tid string
	ts  int64
	cnt int
	tot int
}

func (as *AttachmentStore) transfersTx(tx *sql.Tx) ([]attachTransferRow, error) {
	rows, err := tx.Query(
		`SELECT tid, MIN(ts), COUNT(*), MAX(total) FROM attach WHERE peer = ? GROUP BY tid`, as.peer)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []attachTransferRow
	for rows.Next() {
		var r attachTransferRow
		if err := rows.Scan(&r.tid, &r.ts, &r.cnt, &r.tot); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// makeRoomTx frees budget: oldest incomplete transfer first, then oldest
// complete-but-unconsumed, never the transfer currently being written.
func (as *AttachmentStore) makeRoomTx(tx *sql.Tx, need int64, exclude string) error {
	if need > as.capBytes {
		return errAttachBudget
	}
	var used int64
	if err := tx.QueryRow(
		`SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM attach WHERE peer = ?`, as.peer).Scan(&used); err != nil {
		return err
	}
	for used+need > as.capBytes {
		transfers, err := as.transfersTx(tx)
		if err != nil {
			return err
		}
		victim := pickEviction(transfers, exclude)
		if victim == "" {
			return errAttachBudget
		}
		if _, err := tx.Exec(`DELETE FROM attach WHERE peer = ? AND tid = ?`, as.peer, victim); err != nil {
			return err
		}
		used = 0
		if err := tx.QueryRow(
			`SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM attach WHERE peer = ?`, as.peer).Scan(&used); err != nil {
			return err
		}
	}
	return nil
}

// pickEviction returns the tid of the oldest incomplete transfer, else the
// oldest complete one. Empty transfers hold no bytes and are never targets.
func pickEviction(transfers []attachTransferRow, exclude string) string {
	pick := func(incomplete bool) string {
		best := ""
		for _, t := range transfers {
			if t.tid == exclude || t.cnt == 0 {
				continue
			}
			if (t.cnt < t.tot) != incomplete {
				continue
			}
			if best == "" || t.ts < bestTs(transfers, best) || (t.ts == bestTs(transfers, best) && t.tid < best) {
				best = t.tid
			}
		}
		return best
	}
	if v := pick(true); v != "" {
		return v
	}
	return pick(false)
}

func bestTs(transfers []attachTransferRow, tid string) int64 {
	for _, t := range transfers {
		if t.tid == tid {
			return t.ts
		}
	}
	return 0
}

// Pending lists complete-but-unconsumed transfers (newest first).
func (as *AttachmentStore) Pending() []AttachPending {
	rows, err := as.db.sql.Query(
		`SELECT tid, MIN(ts) AS f, COUNT(*) AS cnt, MAX(total) AS tot
		 FROM attach WHERE peer = ? GROUP BY tid
		 HAVING cnt = tot ORDER BY f DESC`, as.peer)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []AttachPending
	for rows.Next() {
		var (
			tid      string
			ts       int64
			cnt, tot int
		)
		if err := rows.Scan(&tid, &ts, &cnt, &tot); err != nil {
			continue
		}
		out = append(out, AttachPending{Tid: tid, Total: uint16(tot), Ts: ts})
	}
	return out
}

// FetchTid returns one transfer's chunks ordered by index.
func (as *AttachmentStore) FetchTid(tid string) []attachEntry {
	rows, err := as.db.sql.Query(
		`SELECT idx, seq, total, ts, payload FROM attach WHERE peer = ? AND tid = ? ORDER BY idx`,
		as.peer, tid)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []attachEntry
	for rows.Next() {
		var (
			idx, seq, total, ts int64
			payload             []byte
		)
		if err := rows.Scan(&idx, &seq, &total, &ts, &payload); err != nil {
			continue
		}
		out = append(out, attachEntry{
			Tid: tid, Seq: uint64(seq), Index: uint16(idx), Total: uint16(total), Ts: ts,
			Payload: base64.StdEncoding.EncodeToString(payload),
		})
	}
	return out
}

// AckTid frees a transfer after the client consumed it. Idempotent.
func (as *AttachmentStore) AckTid(tid string) error {
	_, err := as.db.sql.Exec(`DELETE FROM attach WHERE peer = ? AND tid = ?`, as.peer, tid)
	return err
}

func (as *AttachmentStore) Bytes() int64 {
	var used int64
	if err := as.db.sql.QueryRow(
		`SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM attach WHERE peer = ?`, as.peer).Scan(&used); err != nil {
		return 0
	}
	return used
}

var errAttachBudget = &attachBudgetError{}

type attachBudgetError struct{}

func (e *attachBudgetError) Error() string { return "attachment budget exceeded" }
