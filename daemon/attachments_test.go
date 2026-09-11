package main

import (
	"encoding/binary"
	"encoding/hex"
	"testing"
)

func filePayload(tid string, index, total uint16, data []byte) []byte {
	raw, _ := hex.DecodeString(tid)
	buf := make([]byte, attachHeaderSize+len(data))
	copy(buf[:16], raw)
	binary.LittleEndian.PutUint16(buf[16:18], index)
	binary.LittleEndian.PutUint16(buf[18:20], total)
	copy(buf[20:], data)
	return buf
}

func TestAttachmentStoreBudgetEvictsOldestIncompleteFirst(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	// budget fits 4 stored chunks (each stored payload is 36 raw bytes)
	as, err := NewAttachmentStore(testDB(t, dir), peer, 36*4)
	if err != nil {
		t.Fatal(err)
	}
	chunk := make([]byte, 16)
	// "ab": incomplete (total 3, only 2 chunks arrive)
	for i := 1; i <= 2; i++ {
		if isNew, err := as.Add("ab", uint16(i), 3, uint64(i), int64(i), filePayload("ab", uint16(i), 3, chunk)); err != nil || !isNew {
			t.Fatalf("add ab chunk %d: new=%v err=%v", i, isNew, err)
		}
	}
	// "cd": complete (2 chunks) — total now fills the 4-chunk budget
	for i := 1; i <= 2; i++ {
		if isNew, err := as.Add("cd", uint16(i), 2, uint64(10+i), int64(10+i), filePayload("cd", uint16(i), 2, chunk)); err != nil || !isNew {
			t.Fatalf("add cd chunk %d: new=%v err=%v", i, isNew, err)
		}
	}
	// one more incomplete chunk pushes over budget -> evict oldest incomplete "ab" wholly
	if _, err := as.Add("ef", 1, 2, uint64(30), int64(30), filePayload("ef", 1, 2, chunk)); err != nil {
		t.Fatalf("add ef: %v", err)
	}
	if got := len(as.FetchTid("ab")); got != 0 {
		t.Fatalf("oldest incomplete transfer ab not fully evicted (chunks=%d)", got)
	}
	if got := len(as.FetchTid("cd")); got != 2 {
		t.Fatalf("complete transfer cd must survive (chunks=%d)", got)
	}
}

func TestAttachmentStoreEvictsOldestCompleteWhenOnlyCompleteRemain(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	as, err := NewAttachmentStore(testDB(t, dir), peer, 36*4)
	if err != nil {
		t.Fatal(err)
	}
	chunk := make([]byte, 16)
	// two complete transfers, 2 chunks each — fills the 4-chunk budget
	for _, tid := range []string{"aa", "bb"} {
		base := int64(0)
		if tid == "bb" {
			base = 10
		}
		for i := 1; i <= 2; i++ {
			if _, err := as.Add(tid, uint16(i), 2, uint64(base+int64(i)), base+int64(i), filePayload(tid, uint16(i), 2, chunk)); err != nil {
				t.Fatal(err)
			}
		}
	}
	// add another chunk -> over budget; only complete transfers remain -> evict oldest complete "aa"
	if _, err := as.Add("cc", 1, 1, uint64(99), int64(99), filePayload("cc", 1, 1, chunk)); err != nil {
		t.Fatal(err)
	}
	if got := len(as.FetchTid("aa")); got != 0 {
		t.Fatalf("oldest complete transfer aa not evicted (chunks=%d)", got)
	}
	if got := len(as.FetchTid("bb")); got != 2 {
		t.Fatalf("newer complete transfer bb must survive (chunks=%d)", got)
	}
}

func TestAttachmentStoreCompleteRetainedUntilAck(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	as, err := NewAttachmentStore(testDB(t, dir), peer, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	chunk := make([]byte, 16)
	for i := 1; i <= 3; i++ {
		if _, err := as.Add("tid1", uint16(i), 3, uint64(i), int64(i), filePayload("tid1", uint16(i), 3, chunk)); err != nil {
			t.Fatal(err)
		}
	}
	pending := as.Pending()
	if len(pending) != 1 || pending[0].Tid != "tid1" || pending[0].Total != 3 {
		t.Fatalf("pending = %+v, want [tid1 total=3]", pending)
	}
	if got := len(as.FetchTid("tid1")); got != 3 {
		t.Fatalf("fetched chunks = %d, want 3", got)
	}
	if err := as.AckTid("tid1"); err != nil {
		t.Fatal(err)
	}
	if len(as.Pending()) != 0 || len(as.FetchTid("tid1")) != 0 {
		t.Fatal("acked transfer must be freed")
	}
}

func TestAttachmentStorePersistsAndReloads(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	chunk := make([]byte, 16)
	as, err := NewAttachmentStore(testDB(t, dir), peer, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= 3; i++ {
		if _, err := as.Add("tid1", uint16(i), 3, uint64(i), int64(i), filePayload("tid1", uint16(i), 3, chunk)); err != nil {
			t.Fatal(err)
		}
	}
	as2, err := NewAttachmentStore(testDB(t, dir), peer, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if len(as2.Pending()) != 1 || len(as2.FetchTid("tid1")) != 3 {
		t.Fatal("store did not survive restart")
	}
	if err := as2.AckTid("tid1"); err != nil {
		t.Fatal(err)
	}
	as3, err := NewAttachmentStore(testDB(t, dir), peer, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if len(as3.Pending()) != 0 {
		t.Fatal("ack did not survive restart")
	}
}

func TestAttachmentStoreFirstChunkUnderBudgetPressure(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	// budget fills to exactly 4 stored chunks before "ef" starts
	as, err := NewAttachmentStore(testDB(t, dir), peer, 36*4)
	if err != nil {
		t.Fatal(err)
	}
	chunk := make([]byte, 16)
	// "gh": incomplete (total 2, only chunk 1 arrives) — the intended eviction victim
	if _, err := as.Add("gh", 1, 2, uint64(20), int64(20), filePayload("gh", 1, 2, chunk)); err != nil {
		t.Fatal(err)
	}
	// "cd": complete (2 chunks)
	for i := 1; i <= 2; i++ {
		if _, err := as.Add("cd", uint16(i), 2, uint64(10+i), int64(10+i), filePayload("cd", uint16(i), 2, chunk)); err != nil {
			t.Fatal(err)
		}
	}
	// "ij": incomplete (1 chunk) — budget now full (144 bytes)
	if _, err := as.Add("ij", 1, 2, uint64(30), int64(30), filePayload("ij", 1, 2, chunk)); err != nil {
		t.Fatal(err)
	}
	// "ef" chunk 1 (of 3): budget pressure while ef's fresh, empty transfer sits
	// in byTid with firstTs=0 — pre-fix it was chosen as the eviction target,
	// orphaning the chunk that this very Add() went on to store.
	if isNew, err := as.Add("ef", 1, 3, uint64(40), int64(40), filePayload("ef", 1, 3, chunk)); err != nil || !isNew {
		t.Fatalf("add ef chunk 1: new=%v err=%v", isNew, err)
	}
	// remaining chunks must still complete the transfer
	for i := 2; i <= 3; i++ {
		if isNew, err := as.Add("ef", uint16(i), 3, uint64(40+i), int64(40+i), filePayload("ef", uint16(i), 3, chunk)); err != nil || !isNew {
			t.Fatalf("add ef chunk %d: new=%v err=%v", i, isNew, err)
		}
	}
	if got := len(as.FetchTid("ef")); got != 3 {
		t.Fatalf("FetchTid(ef) = %d chunks, want 3 (transfer stranded)", got)
	}
	found := false
	for _, p := range as.Pending() {
		if p.Tid == "ef" {
			found = true
		}
	}
	if !found {
		t.Fatalf("ef never reached Pending(): %+v", as.Pending())
	}
	// the intended victim ("gh", oldest incomplete) is what got evicted
	if got := len(as.FetchTid("gh")); got != 0 {
		t.Fatalf("gh should have been the eviction victim (chunks=%d)", got)
	}
}

func TestAttachmentStoreTightBudgetFailsCleanly(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	// budget fits 2 chunks; the transfer needs 3 — larger than the whole budget
	// once it starts, so Add must fail cleanly instead of stranding chunks
	as, err := NewAttachmentStore(testDB(t, dir), peer, 36*2)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := as.Add("ef", 1, 3, uint64(1), int64(1), filePayload("ef", 1, 3, make([]byte, 16))); err != nil {
		t.Fatalf("first chunk should fit: %v", err)
	}
	if _, err := as.Add("ef", 2, 3, uint64(2), int64(2), filePayload("ef", 2, 3, make([]byte, 16))); err != nil {
		t.Fatalf("second chunk should fit: %v", err)
	}
	if _, err := as.Add("ef", 3, 3, uint64(3), int64(3), filePayload("ef", 3, 3, make([]byte, 16))); err == nil {
		t.Fatal("Add must fail cleanly when the budget cannot hold the chunk")
	}
	if got := len(as.FetchTid("ef")); got != 2 {
		t.Fatalf("stored chunks = %d, want 2 (the two that fit)", got)
	}
	// the incomplete transfer survives a restart, still tracked and evictable
	as2, err := NewAttachmentStore(testDB(t, dir), peer, 36*2)
	if err != nil {
		t.Fatal(err)
	}
	if got := len(as2.FetchTid("ef")); got != 2 {
		t.Fatalf("stored chunks after reload = %d, want 2", got)
	}
}

func TestAttachmentStoreDedupAndBudgetStable(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	as, err := NewAttachmentStore(testDB(t, dir), peer, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	chunk := make([]byte, 16)
	if isNew, err := as.Add("tid1", 1, 2, uint64(1), int64(1), filePayload("tid1", 1, 2, chunk)); err != nil || !isNew {
		t.Fatalf("first: new=%v err=%v", isNew, err)
	}
	before := as.Bytes()
	if isNew, _ := as.Add("tid1", 1, 2, uint64(1), int64(1), filePayload("tid1", 1, 2, chunk)); isNew {
		t.Fatal("duplicate chunk reported new")
	}
	if as.Bytes() != before {
		t.Fatal("duplicate chunk must not grow the budget")
	}
}
