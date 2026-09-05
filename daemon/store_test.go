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
	if isNew, _ := in.Add(1, 10, []byte("replay of trimmed")); isNew {
		t.Fatal("replay of a trimmed (already delivered) seq must be rejected")
	}
	if got := in.Oldest(); got != 2 {
		t.Fatalf("oldest after replay = %d, want 2", got)
	}
	if isNew, _ := in.Add(0, 5, []byte("never-seen late arrival")); !isNew {
		t.Fatal("never-seen seq below the retained window must be accepted")
	}
	afterAll := in.After(0)
	if len(afterAll) != 3 || afterAll[0].Seq != 2 || afterAll[2].Seq != 4 {
		t.Fatalf("after(0) = %+v", afterAll)
	}
	if in.Latest() != 4 {
		t.Fatalf("latest = %d, want 4", in.Latest())
	}
}

func TestInboxOutOfOrderBothAccepted(t *testing.T) {
	dir := t.TempDir()
	in, err := NewInboxStore(dir, "peer", 3)
	if err != nil {
		t.Fatal(err)
	}
	if isNew, _ := in.Add(12, 1200, []byte("twelve")); !isNew {
		t.Fatal("seq 12 not new")
	}
	if isNew, _ := in.Add(11, 1100, []byte("eleven")); !isNew {
		t.Fatal("out-of-order seq 11 must be accepted as new (never seen)")
	}
	if isNew, _ := in.Add(12, 1200, []byte("twelve again")); isNew {
		t.Fatal("replay of 12 reported new")
	}
	if isNew, _ := in.Add(11, 1100, []byte("eleven again")); isNew {
		t.Fatal("replay of 11 reported new")
	}
	if in.Oldest() != 11 || in.Latest() != 12 {
		t.Fatalf("window = %d..%d, want 11..12", in.Oldest(), in.Latest())
	}
}

func TestInboxTrimmedReplayRejectedAndLateArrivalAccepted(t *testing.T) {
	dir := t.TempDir()
	in, err := NewInboxStore(dir, "peer", 2)
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []uint64{1, 2, 3} {
		if isNew, _ := in.Add(s, int64(s), []byte("x")); !isNew {
			t.Fatalf("add %d not new", s)
		}
	}
	if isNew, _ := in.Add(1, 10, []byte("trimmed replay")); isNew {
		t.Fatal("replay of trimmed seq 1 must be rejected")
	}
	if isNew, _ := in.Add(2, 20, []byte("trimmed replay 2")); isNew {
		t.Fatal("replay of trimmed seq 2 must be rejected")
	}

	// A seq that was never seen, arriving below the retained window after trim.
	if isNew, _ := in.Add(0, 5, []byte("late, never-seen")); !isNew {
		t.Fatal("never-seen low seq must be accepted")
	}
	if isNew, _ := in.Add(0, 5, []byte("replay of the late arrival")); isNew {
		t.Fatal("replay of the accepted late arrival must be rejected")
	}
}

func TestInboxSeenSetSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	peer := "peer"
	in, err := NewInboxStore(dir, peer, 2)
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []uint64{1, 2, 3} {
		if isNew, _ := in.Add(s, int64(s), []byte("x")); !isNew {
			t.Fatalf("add %d not new", s)
		}
	}

	in2, err := NewInboxStore(dir, peer, 2)
	if err != nil {
		t.Fatal(err)
	}
	if isNew, _ := in2.Add(1, 10, []byte("replay after restart")); isNew {
		t.Fatal("trimmed replay must still be rejected after restart")
	}
	if isNew, _ := in2.Add(4, 40, []byte("forward seq")); !isNew {
		t.Fatal("forward seq after restart not new")
	}
	if isNew, _ := in2.Add(3, 30, []byte("replay in window")); isNew {
		t.Fatal("replay of retained seq 3 must be rejected after restart")
	}
}

func TestInboxDedupLargeN(t *testing.T) {
	dir := t.TempDir()
	const n = 10000
	in, err := NewInboxStore(dir, "peer", n)
	if err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= n; i++ {
		if isNew, _ := in.Add(uint64(i), int64(i), []byte("x")); !isNew {
			t.Fatalf("add %d not new", i)
		}
	}
	for _, replay := range []uint64{1, n / 2, n} {
		if isNew, _ := in.Add(replay, int64(replay), []byte("replay")); isNew {
			t.Fatalf("replay %d reported new", replay)
		}
	}
	if isNew, _ := in.Add(uint64(n+1), int64(n+1), []byte("next")); !isNew {
		t.Fatal("seq n+1 not new")
	}
	if in.Latest() != uint64(n+1) {
		t.Fatalf("latest = %d, want %d", in.Latest(), n+1)
	}
	if in.Oldest() != 2 {
		t.Fatalf("oldest = %d, want 2 (1 trimmed)", in.Oldest())
	}
}
