package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func mustB64(b []byte) string {
	return base64.StdEncoding.EncodeToString(b)
}

func osWriteLegacyJournal(t *testing.T, dir, peer string) {
	t.Helper()
	if err := os.MkdirAll(journalDir(dir), 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(journalDir(dir), peer+".jsonl")
	// a pre-0.5.0 entry has no "type" field
	if err := os.WriteFile(path, []byte(`{"seq":1,"ts":111,"payload":"eA=="}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestFileChunkDeliveryRetrievalAndAck(t *testing.T) {
	dirA, dirB := t.TempDir(), t.TempDir()
	nodeA, err := StartNode(dirA, nil, []string{"tcp://127.0.0.1:0"}, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	defer nodeA.Stop()
	var peerURL string
	for _, l := range nodeA.listeners {
		peerURL = "tcp://" + l.Addr().String()
	}
	nodeB, err := StartNode(dirB, []string{peerURL}, nil, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	defer nodeB.Stop()
	waitForPeerUp(t, nodeB.Core, 15*time.Second)

	mailA := newTestMail(t, dirA, nodeA)
	mailB := newTestMail(t, dirB, nodeB)
	mailA.Attach(nodeA)
	mailB.Attach(nodeB)

	delivered := make(chan struct{}, 8)
	mailA.OnDeliverFile = func(from ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
		delivered <- struct{}{}
	}
	acked := make(chan struct{}, 8)
	mailB.OnAcked = func(to ed25519.PublicKey, seq uint64) {
		acked <- struct{}{}
	}

	tid := "00112233445566778899aabbccddeeff"
	chunk := filePayload(tid, 0, 2, []byte("hello file"))
	chunk2 := filePayload(tid, 1, 2, []byte("world file"))
	if _, err := mailB.SendFile(nodeA.Pub, chunk); err != nil {
		t.Fatal(err)
	}
	if _, err := mailB.SendFile(nodeA.Pub, chunk2); err != nil {
		t.Fatal(err)
	}

	for i := 0; i < 2; i++ {
		waitFor(t, 20*time.Second, "file chunk delivery", delivered)
	}
	for i := 0; i < 2; i++ {
		waitFor(t, 20*time.Second, "file chunk ack", acked)
	}

	// chunks must NOT have touched the text inbox
	if msgs := mailA.boxes[hexString(nodeB.Pub)].inbox.After(0); len(msgs) != 0 {
		t.Fatalf("file chunks leaked into the inbox: %d entries", len(msgs))
	}

	pending, err := mailA.PendingAttachments(hexString(nodeB.Pub))
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 1 || pending[0].Tid != tid || pending[0].Total != 2 {
		t.Fatalf("pending = %+v, want [tid1 total=2]", pending)
	}
	fetched, err := mailA.FetchAttachment(hexString(nodeB.Pub), tid)
	if err != nil {
		t.Fatal(err)
	}
	if len(fetched) != 2 || fetched[0].Index != 0 || fetched[1].Index != 1 {
		t.Fatalf("fetched = %+v, want chunks 1..2 in order", fetched)
	}
	if fetched[0].Payload != mustB64(chunk) || fetched[1].Payload != mustB64(chunk2) {
		t.Fatal("fetched chunk payloads do not match what was sent")
	}

	if err := mailA.AckAttachment(hexString(nodeB.Pub), tid); err != nil {
		t.Fatal(err)
	}
	pending, _ = mailA.PendingAttachments(hexString(nodeB.Pub))
	if len(pending) != 0 {
		t.Fatalf("pending after ack = %+v, want empty", pending)
	}
}

func TestFileChunkBlockedDropsLikeText(t *testing.T) {
	dirA, dirB := t.TempDir(), t.TempDir()
	nodeA, err := StartNode(dirA, nil, []string{"tcp://127.0.0.1:0"}, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	defer nodeA.Stop()
	var peerURL string
	for _, l := range nodeA.listeners {
		peerURL = "tcp://" + l.Addr().String()
	}
	nodeB, err := StartNode(dirB, []string{peerURL}, nil, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	defer nodeB.Stop()
	waitForPeerUp(t, nodeB.Core, 15*time.Second)

	mailA := newTestMail(t, dirA, nodeA)
	mailB := newTestMail(t, dirB, nodeB)
	mailA.Attach(nodeA)
	mailB.Attach(nodeB)

	delivered := make(chan struct{}, 8)
	mailA.OnDeliverFile = func(from ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
		delivered <- struct{}{}
	}
	if err := mailA.Block(nodeB.Pub); err != nil {
		t.Fatal(err)
	}
	if _, err := mailB.SendFile(nodeA.Pub, filePayload("tid1", 1, 2, []byte("nope"))); err != nil {
		t.Fatal(err)
	}

	time.Sleep(1500 * time.Millisecond)
	select {
	case <-delivered:
		t.Fatal("blocked peer's file chunk was delivered")
	default:
	}
	pending, _ := mailA.PendingAttachments(hexString(nodeB.Pub))
	if len(pending) != 0 {
		t.Fatalf("blocked peer left attachments: %+v", pending)
	}
	if pending := mailB.boxes[hexString(nodeA.Pub)].journal.Pending(); len(pending) == 0 {
		t.Fatal("sender journal empty: blocked file chunk was acked")
	}
}

func TestFileChunkDedupFiresDeliverOnce(t *testing.T) {
	dirA, dirB := t.TempDir(), t.TempDir()
	nodeA, err := StartNode(dirA, nil, []string{"tcp://127.0.0.1:0"}, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	defer nodeA.Stop()
	var peerURL string
	for _, l := range nodeA.listeners {
		peerURL = "tcp://" + l.Addr().String()
	}
	nodeB, err := StartNode(dirB, []string{peerURL}, nil, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	defer nodeB.Stop()
	waitForPeerUp(t, nodeB.Core, 15*time.Second)

	mailA := newTestMail(t, dirA, nodeA)
	mailB := newTestMail(t, dirB, nodeB)
	mailA.Attach(nodeA)
	mailB.Attach(nodeB)

	count := 0
	mailA.OnDeliverFile = func(from ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
		count++
	}
	chunk := filePayload("tid1", 1, 2, []byte("dup me"))
	for i := 0; i < 3; i++ {
		if _, err := mailB.SendFile(nodeA.Pub, chunk); err != nil {
			t.Fatal(err)
		}
	}
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) && count < 1 {
		time.Sleep(50 * time.Millisecond)
	}
	time.Sleep(500 * time.Millisecond)
	if count != 1 {
		t.Fatalf("duplicate chunk delivered %d times, want 1", count)
	}
}

func TestJournalTypeRoundTrip(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	j, err := NewOutboxJournal(dir, peer)
	if err != nil {
		t.Fatal(err)
	}
	seq, _ := j.NextSeq()
	if err := j.QueueAs(seq, 111, []byte("file chunk"), TypeFile); err != nil {
		t.Fatal(err)
	}
	j2, err := NewOutboxJournal(dir, peer)
	if err != nil {
		t.Fatal(err)
	}
	pending := j2.Pending()
	if len(pending) != 1 || pending[0].Type != TypeFile {
		t.Fatalf("journal type = %v, want TypeFile", pending[0].Type)
	}
}

func TestJournalLegacyEntriesNormalizeToMsg(t *testing.T) {
	dir := t.TempDir()
	peer := "aabb"
	osWriteLegacyJournal(t, dir, peer)
	j, err := NewOutboxJournal(dir, peer)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range j.Pending() {
		if e.Type != TypeMsg {
			t.Fatalf("legacy entry type = %v, want TypeMsg", e.Type)
		}
	}
}
