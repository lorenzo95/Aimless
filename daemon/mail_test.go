package main

import (
	"crypto/ed25519"
	"strings"
	"testing"
	"time"
)

// Regression for the false "marked-sent-without-being-sent" bug: a chunk whose
// enqueue fails (bulk queue full) must stay eligible for the very next flush
// pass instead of being parked by a SentAt stamp for an enqueue that never
// happened. The bulk channel itself is the observable: no writeLoop is run, so
// whatever flushPeer managed to enqueue stays visible in it.
func TestFlushPeerDoesNotMarkFailedEnqueues(t *testing.T) {
	dir := t.TempDir()
	peer := strings.Repeat("ab", 32)

	// bulk capacity 1: the first chunk enqueues, the second bounces
	n := &Node{
		control: make(chan outbound, 64),
		bulk:    make(chan outbound, 1),
		writeFn: func(_ ed25519.PublicKey, _ []byte) {},
	}
	j, err := NewOutboxJournal(testDB(t, dir), peer)
	if err != nil {
		t.Fatal(err)
	}
	seq1, _ := j.NextSeq()
	if err := j.QueueAs(seq1, 100, []byte("chunk-1"), TypeFile); err != nil {
		t.Fatal(err)
	}
	seq2, _ := j.NextSeq()
	if err := j.QueueAs(seq2, 200, []byte("chunk-2"), TypeFile); err != nil {
		t.Fatal(err)
	}

	m := &Mail{
		boxes:         map[string]*Mailbox{peer: {journal: j}},
		node:          n,
		retryInterval: 2 * time.Second,
		blocked:       make(map[string]struct{}),
	}

	// pass 1: seq1 enqueues (marked); seq2 bounces (must stay unmarked)
	m.flushPeer(peer)
	if got := len(n.bulk); got != 1 {
		t.Fatalf("bulk queue after pass 1 = %d, want 1", got)
	}
	entries := j.Pending()
	bySeq := map[uint64]journalEntry{}
	for _, e := range entries {
		bySeq[e.Seq] = e
	}
	if bySeq[seq1].SentAt == 0 {
		t.Fatal("successfully enqueued chunk must be marked sent")
	}
	if bySeq[seq2].SentAt != 0 {
		t.Fatal("chunk that bounced off a full queue must NOT be marked sent")
	}

	// make room, then flush again: the failed chunk must be RE-ATTEMPTED (its
	// SentAt was never stamped), enqueue, and only now get marked.
	drainQueue(n.bulk)
	m.flushPeer(peer)
	if got := len(n.bulk); got != 1 {
		t.Fatalf("bulk queue after pass 2 = %d, want 1 (the retried chunk)", got)
	}
	out := <-n.bulk
	if !strings.Contains(string(out.data), "chunk-2") {
		t.Fatal("retried packet is not the failed chunk")
	}
	if got := sentAtFor(j, seq2); got == 0 {
		t.Fatal("chunk enqueued on the retry must be marked sent")
	}
}

func sentAtFor(j *OutboxJournal, seq uint64) int64 {
	for _, e := range j.Pending() {
		if e.Seq == seq {
			return e.SentAt
		}
	}
	return -1
}

func drainQueue(q chan outbound) {
	for {
		select {
		case <-q:
			continue
		default:
		}
		return
	}
}
