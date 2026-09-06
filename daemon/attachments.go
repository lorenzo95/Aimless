package main

import (
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// TypeFile chunk wire payload layout: a 20-byte unencrypted routing header
// (transfer id + chunk index/total) followed by the E2E-sealed chunk body. The
// daemon reads only the header — enough to key the AttachmentStore and judge
// completeness — while filename/hash/data stay encrypted end to end.
const attachHeaderSize = 20

func attachmentDir(datadir string) string {
	return filepath.Join(datadir, "attachments")
}

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

type attachTransfer struct {
	total    uint16
	firstTs  int64
	chunks   map[uint16]attachEntry
	complete bool
}

// AttachmentStore is a per-peer landing buffer for incoming TypeFile chunks.
// It retains a transfer until the client acknowledges consumption (AckTid),
// so files are as durable as text: an offline client can catch up later. A
// per-peer byte budget bounds disk use; eviction drops the oldest incomplete
// transfer wholesale first (it is unusable without its missing chunks), then
// the oldest complete-but-unconsumed one.
type AttachmentStore struct {
	mu       sync.Mutex
	path     string
	capBytes int64
	entries  []attachEntry
	byTid    map[string]*attachTransfer
	bytes    int64
}

func NewAttachmentStore(datadir, peerHex string, capBytes int64) (*AttachmentStore, error) {
	dir := attachmentDir(datadir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	as := &AttachmentStore{
		path:     filepath.Join(dir, peerHex+".jsonl"),
		capBytes: capBytes,
		byTid:    make(map[string]*attachTransfer),
	}
	if err := as.load(); err != nil {
		return nil, err
	}
	return as, nil
}

func (as *AttachmentStore) load() error {
	data, err := os.ReadFile(as.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if line == "" {
			continue
		}
		var e attachEntry
		if err := json.Unmarshal([]byte(line), &e); err != nil {
			return err
		}
		as.entries = append(as.entries, e)
		as.ingestLocked(e)
	}
	return nil
}

func (as *AttachmentStore) ingestLocked(e attachEntry) {
	t := as.byTid[e.Tid]
	if t == nil {
		t = &attachTransfer{total: e.Total, chunks: make(map[uint16]attachEntry)}
		as.byTid[e.Tid] = t
	}
	if _, exists := t.chunks[e.Index]; exists {
		return
	}
	if t.firstTs == 0 || e.Ts < t.firstTs {
		t.firstTs = e.Ts
	}
	t.chunks[e.Index] = e
	if len(t.chunks) == int(t.total) {
		t.complete = true
	}
	as.bytes += int64(len(e.Payload))
}

// Add stores one chunk (dedup by tid+index) after enforcing the byte budget.
// It returns isNew=false for a duplicate chunk.
func (as *AttachmentStore) Add(tid string, index, total uint16, seq uint64, ts int64, payload []byte) (bool, error) {
	as.mu.Lock()
	defer as.mu.Unlock()
	t := as.byTid[tid]
	if t == nil {
		t = &attachTransfer{total: total, chunks: make(map[uint16]attachEntry)}
		as.byTid[tid] = t
	}
	if _, exists := t.chunks[index]; exists {
		return false, nil
	}
	if err := as.makeRoomLocked(int64(len(payload)), tid); err != nil {
		// Budget failure: keep any chunks of this transfer already stored (they
		// stay tracked and evictable); only drop the empty husk a failed first
		// chunk would otherwise leave behind.
		if t2 := as.byTid[tid]; t2 != nil && len(t2.chunks) == 0 {
			delete(as.byTid, tid)
		}
		return false, err
	}
	// makeRoom may have evicted and recreated bookkeeping — re-fetch so the
	// chunk can never land in a struct nothing references anymore.
	t = as.byTid[tid]
	if t == nil {
		t = &attachTransfer{total: total, chunks: make(map[uint16]attachEntry)}
		as.byTid[tid] = t
	}
	e := attachEntry{Tid: tid, Seq: seq, Index: index, Total: total, Ts: ts,
		Payload: base64.StdEncoding.EncodeToString(payload)}
	if t.firstTs == 0 || ts < t.firstTs {
		t.firstTs = ts
	}
	t.chunks[index] = e
	if len(t.chunks) == int(t.total) {
		t.complete = true
	}
	as.bytes += int64(len(e.Payload))
	as.entries = append(as.entries, e)
	// Append-only: a full rewrite per chunk would be O(n²) across a transfer
	// (the receiver ACKs each chunk, and the ACK contract requires the chunk on
	// disk first). Eviction/ack rewrite the file wholesale instead.
	return true, as.appendLocked(e)
}

func (as *AttachmentStore) appendLocked(e attachEntry) error {
	f, err := os.OpenFile(as.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	line, err := json.Marshal(e)
	if err != nil {
		return err
	}
	_, err = f.Write(append(line, '\n'))
	return err
}

// makeRoomLocked frees budget: oldest incomplete transfer wholesale first,
// then oldest complete-but-unconsumed. The transfer being added (exclude) is
// never an eviction target — evicting it mid-Add would strand its earlier
// chunks and orphan the chunk being stored. Returns an error only if a single
// chunk is larger than the whole budget (excluding the current transfer).
func (as *AttachmentStore) makeRoomLocked(need int64, exclude string) error {
	if need > as.capBytes {
		return errAttachBudget
	}
	for as.bytes+need > as.capBytes {
		tid := as.oldestLocked(false, exclude)
		if tid == "" {
			tid = as.oldestLocked(true, exclude)
		}
		if tid == "" {
			return errAttachBudget
		}
		as.removeTransferLocked(tid)
	}
	return nil
}

// oldestLocked returns the tid of the oldest transfer whose completion state
// matches `complete`, or "" if none. Empty transfers (created but not yet
// stored) hold no bytes and must never be eviction targets: "evicting" one
// frees nothing and orphans the chunk Add() is about to store into it.
func (as *AttachmentStore) oldestLocked(complete bool, exclude string) string {
	var best string
	var bestTs int64
	for tid, t := range as.byTid {
		if len(t.chunks) == 0 {
			continue
		}
		if tid == exclude {
			continue
		}
		if t.complete != complete {
			continue
		}
		if best == "" || t.firstTs < bestTs || (t.firstTs == bestTs && tid < best) {
			best, bestTs = tid, t.firstTs
		}
	}
	return best
}

func (as *AttachmentStore) removeTransferLocked(tid string) {
	t := as.byTid[tid]
	if t == nil {
		return
	}
	keep := as.entries[:0]
	for _, e := range as.entries {
		if e.Tid == tid {
			as.bytes -= int64(len(e.Payload))
			continue
		}
		keep = append(keep, e)
	}
	as.entries = keep
	delete(as.byTid, tid)
}

// AttachPending describes a complete-but-unconsumed transfer (the client has
// not yet acked it), for the pendingattachments listing.
type AttachPending struct {
	Tid   string `json:"tid"`
	Total uint16 `json:"total"`
	Ts    int64  `json:"ts"`
}

// Pending lists complete-but-unconsumed transfers (newest first).
func (as *AttachmentStore) Pending() []AttachPending {
	as.mu.Lock()
	defer as.mu.Unlock()
	var out []AttachPending
	seen := make(map[string]bool)
	for i := len(as.entries) - 1; i >= 0; i-- {
		e := as.entries[i]
		if seen[e.Tid] {
			continue
		}
		seen[e.Tid] = true
		t := as.byTid[e.Tid]
		if t != nil && t.complete {
			out = append(out, AttachPending{Tid: e.Tid, Total: e.Total, Ts: e.Ts})
		}
	}
	return out
}

// FetchTid returns one transfer's chunks ordered by index.
func (as *AttachmentStore) FetchTid(tid string) []attachEntry {
	as.mu.Lock()
	defer as.mu.Unlock()
	t := as.byTid[tid]
	if t == nil {
		return nil
	}
	out := make([]attachEntry, 0, len(t.chunks))
	for _, e := range t.chunks {
		out = append(out, e)
	}
	sort.Slice(out, func(i, k int) bool { return out[i].Index < out[k].Index })
	return out
}

// AckTid frees a transfer after the client has consumed it. Idempotent.
func (as *AttachmentStore) AckTid(tid string) error {
	as.mu.Lock()
	defer as.mu.Unlock()
	if as.byTid[tid] == nil {
		return nil
	}
	as.removeTransferLocked(tid)
	return as.persistLocked()
}

func (as *AttachmentStore) Bytes() int64 {
	as.mu.Lock()
	defer as.mu.Unlock()
	return as.bytes
}

func (as *AttachmentStore) persistLocked() error {
	data := make([]byte, 0, 256*len(as.entries))
	for _, e := range as.entries {
		line, err := json.Marshal(e)
		if err != nil {
			return err
		}
		data = append(data, append(line, '\n')...)
	}
	tmp := as.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, as.path)
}

var errAttachBudget = &attachBudgetError{}

type attachBudgetError struct{}

func (e *attachBudgetError) Error() string { return "attachment budget exceeded" }
