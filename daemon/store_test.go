package main

import (
	"testing"
)

func TestOutboxJournalPersistAndAck(t *testing.T) {
	dir := t.TempDir()
	peerHex := "aabb"
	j, err := NewOutboxJournal(dir, peerHex)
	if err != nil {
		t.Fatal(err)
	}
	seq1, _ := j.NextSeq()
	seq2, _ := j.NextSeq()
	if seq1 != 1 || seq2 != 2 {
		t.Fatalf("seqs = %d,%d want 1,2", seq1, seq2)
	}
	if err := j.Queue(seq1, 111, []byte("one")); err != nil {
		t.Fatal(err)
	}
	if err := j.Queue(seq2, 222, []byte("two")); err != nil {
		t.Fatal(err)
	}
	if pending := j.Pending(); len(pending) != 2 {
		t.Fatalf("pending = %d, want 2", len(pending))
	}

	removed, err := j.Ack(seq1)
	if err != nil || !removed {
		t.Fatalf("ack: removed=%v err=%v", removed, err)
	}
	if pending := j.Pending(); len(pending) != 1 || pending[0].Seq != seq2 {
		t.Fatalf("pending after ack: %+v", pending)
	}

	j2, err := NewOutboxJournal(dir, peerHex)
	if err != nil {
		t.Fatal(err)
	}
	if pending := j2.Pending(); len(pending) != 1 || pending[0].Seq != seq2 {
		t.Fatalf("reloaded pending: %+v", pending)
	}
	seq3, _ := j2.NextSeq()
	if seq3 != 3 {
		t.Fatalf("reloaded nextSeq gave %d, want 3 (no seq reuse)", seq3)
	}
	if removed, err := j2.Ack(999); err != nil || removed {
		t.Fatalf("ack unknown seq: removed=%v err=%v", removed, err)
	}
}

func TestInboxTrimAndGapFloor(t *testing.T) {
	dir := t.TempDir()
	peerHex := "ccdd"
	in, err := NewInboxStore(dir, peerHex, 3)
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []uint64{1, 2, 3} {
		if isNew, err := in.Add(s, int64(s)*10, []byte("x")); err != nil || !isNew {
			t.Fatalf("add %d: isNew=%v err=%v", s, isNew, err)
		}
	}
	if isNew, _ := in.Add(2, 20, []byte("x")); isNew {
		t.Fatal("duplicate add reported new")
	}
	if isNew, _ := in.Add(4, 40, []byte("x")); !isNew {
		t.Fatal("add 4 not new")
	}
	if in.Oldest() != 2 {
		t.Fatalf("oldest = %d, want 2 (trimmed)", in.Oldest())
	}
	after := in.After(3)
	if len(after) != 1 || after[0].Seq != 4 {
		t.Fatalf("after(3) = %+v, want [4]", after)
	}
	if isNew, _ := in.Add(1, 10, []byte("late gap fill")); !isNew {
		t.Fatal("gap fill add 1 not new")
	}
	if got := in.Oldest(); got != 2 {
		t.Fatalf("oldest after trim = %d, want 2", got)
	}
	afterAll := in.After(0)
	if len(afterAll) != 3 || afterAll[0].Seq != 2 || afterAll[2].Seq != 4 {
		t.Fatalf("after(0) = %+v", afterAll)
	}
	if in.Latest() != 4 {
		t.Fatalf("latest = %d, want 4", in.Latest())
	}
}
