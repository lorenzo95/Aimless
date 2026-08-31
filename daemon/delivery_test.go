package main

import (
	"crypto/ed25519"
	"testing"
	"time"
)

func newTestMail(t *testing.T, datadir string, node *Node) *Mail {
	t.Helper()
	mail, err := NewMail(datadir, 50, 250*time.Millisecond)
	if err != nil {
		t.Fatalf("NewMail: %v", err)
	}
	wireMail(node, mail)
	mail.Attach(node)
	return mail
}

func waitFor(t *testing.T, timeout time.Duration, what string, ch chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(timeout):
		t.Fatalf("timeout waiting for %s", what)
	}
}

func TestMailDeliverAndAck(t *testing.T) {
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
	var gotPayload []byte
	var gotSeq uint64
	mailB.OnDeliver = func(from ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
		gotPayload = payload
		gotSeq = seq
		delivered <- struct{}{}
	}
	acked := make(chan struct{}, 8)
	var ackedSeq uint64
	mailA.OnAcked = func(to ed25519.PublicKey, seq uint64) {
		ackedSeq = seq
		acked <- struct{}{}
	}

	seq, err := mailA.SendMsg(nodeB.Pub, []byte("mail hello"))
	if err != nil {
		t.Fatalf("SendMsg: %v", err)
	}
	if seq != 1 {
		t.Fatalf("seq = %d, want 1", seq)
	}

	waitFor(t, 20*time.Second, "delivery at B", delivered)
	if string(gotPayload) != "mail hello" || gotSeq != 1 {
		t.Fatalf("delivered seq=%d payload=%q", gotSeq, gotPayload)
	}
	waitFor(t, 20*time.Second, "ack at A", acked)
	if ackedSeq != 1 {
		t.Fatalf("acked seq = %d, want 1", ackedSeq)
	}
	if pending := mailA.boxes[hexString(nodeB.Pub)].journal.Pending(); len(pending) != 0 {
		t.Fatalf("journal not empty after ack: %+v", pending)
	}
}

func TestMailOfflineDelivery(t *testing.T) {
	dirA, dirB := t.TempDir(), t.TempDir()
	privB, err := LoadOrCreateKey(dirB)
	if err != nil {
		t.Fatal(err)
	}
	pubB32 := privB.Public().(ed25519.PublicKey)

	nodeA, err := StartNode(dirA, nil, []string{"tcp://127.0.0.1:0"}, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	defer nodeA.Stop()
	var peerURL string
	for _, l := range nodeA.listeners {
		peerURL = "tcp://" + l.Addr().String()
	}

	mailA := newTestMail(t, dirA, nodeA)
	mailA.Attach(nodeA)

	seq, err := mailA.SendMsg(pubB32, []byte("while you were out"))
	if err != nil {
		t.Fatalf("SendMsg while B offline: %v", err)
	}
	if len(mailA.boxes[hexString(pubB32)].journal.Pending()) != 1 {
		t.Fatal("journal should hold 1 pending message")
	}

	nodeB, err := StartNode(dirB, []string{peerURL}, nil, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	defer nodeB.Stop()
	waitForPeerUp(t, nodeB.Core, 15*time.Second)

	mailB := newTestMail(t, dirB, nodeB)
	mailB.Attach(nodeB)

	delivered := make(chan struct{}, 8)
	var gotSeq uint64
	var gotPayload []byte
	mailB.OnDeliver = func(from ed25519.PublicKey, s uint64, ts int64, payload []byte) {
		gotSeq, gotPayload = s, payload
		delivered <- struct{}{}
	}
	acked := make(chan struct{}, 8)
	mailA.OnAcked = func(to ed25519.PublicKey, s uint64) {
		acked <- struct{}{}
	}

	waitFor(t, 20*time.Second, "offline message delivery", delivered)
	if gotSeq != seq || string(gotPayload) != "while you were out" {
		t.Fatalf("delivered seq=%d payload=%q, want seq=%d", gotSeq, gotPayload, seq)
	}
	waitFor(t, 20*time.Second, "ack after reconnect", acked)
	if len(mailA.boxes[hexString(pubB32)].journal.Pending()) != 0 {
		t.Fatal("journal should be empty after offline delivery ack")
	}
}

func TestMailHistoryReplay(t *testing.T) {
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

	acked := make(chan struct{}, 32)
	mailA.OnAcked = func(to ed25519.PublicKey, seq uint64) {
		acked <- struct{}{}
	}
	for i := 1; i <= 5; i++ {
		if _, err := mailA.SendMsg(nodeB.Pub, []byte{byte('a' + i - 1)}); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < 5; i++ {
		waitFor(t, 20*time.Second, "ack", acked)
	}

	msgs, oldest, latest, err := mailB.History(hexString(nodeA.Pub), 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 5 || oldest != 1 || latest != 5 {
		t.Fatalf("history: n=%d oldest=%d latest=%d, want 5/1/5", len(msgs), oldest, latest)
	}
	msgs, _, _, err = mailB.History(hexString(nodeA.Pub), 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 2 || msgs[0].Seq != 4 || msgs[1].Seq != 5 {
		t.Fatalf("history after 3 = %+v, want seqs 4,5", msgs)
	}
}

func TestMailSeqPersistsAcrossRestart(t *testing.T) {
	dirA := t.TempDir()
	peer := make(ed25519.PublicKey, 32)

	node1, err := StartNode(dirA, nil, nil, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	mail1 := newTestMail(t, dirA, node1)
	seq, err := mail1.SendMsg(peer, []byte("m1"))
	if err != nil {
		t.Fatal(err)
	}
	if seq != 1 {
		t.Fatalf("first seq = %d, want 1", seq)
	}
	node1.Stop()

	node2, err := StartNode(dirA, nil, nil, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	defer node2.Stop()
	mail2 := newTestMail(t, dirA, node2)
	seq, err = mail2.SendMsg(peer, []byte("m2"))
	if err != nil {
		t.Fatal(err)
	}
	if seq != 2 {
		t.Fatalf("seq after restart = %d, want 2 (no reuse)", seq)
	}
}
