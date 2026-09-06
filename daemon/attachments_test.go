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
	// budget fits 4 stored chunks (each stored payload is 48 chars of base64)
	as, err := NewAttachmentStore(dir, peer, 48*4)
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
	as, err := NewAttachmentStore(dir, peer, 48*4)
	if err != nil {
		t.Fatal(err)
	}
	chunk := make([]byte, 16)
	// two complete transfers, 2 chunks each — fills the 4-chunk budget
	for _, tid := range []string{"aa", "bb"} {
		for i := 1; i <= 2; i++ {
			if _, err := as.Add(tid, uint16(i), 2, uint64(i), int64(i), filePayload(tid, uint16(i), 2, chunk)); err != nil {
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
	as, err := NewAttachmentStore(dir, peer, 1<<20)
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
	as, err := NewAttachmentStore(dir, peer, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= 3; i++ {
		if _, err := as.Add("tid1", uint16(i), 3, uint64(i), int64(i), filePayload("tid1", uint16(i), 3, chunk)); err != nil {
			t.Fatal(err)
		}
	}
	as2, err := NewAttachmentStore(dir, peer, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if len(as2.Pending()) != 1 || len(as2.FetchTid("tid1")) != 3 {
		t.Fatal("store did not survive restart")
	}
	if err := as2.AckTid("tid1"); err != nil {
		t.Fatal(err)
	}
	as3, err := NewAttachmentStore(dir, peer, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if len(as3.Pending()) != 0 {
		t.Fatal("ack did not survive restart")
	}
}

func TestAttachmentStoreDedupAndBudgetStable(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	as, err := NewAttachmentStore(dir, peer, 1<<20)
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