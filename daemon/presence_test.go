package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"testing"
	"time"
)

func presenceFixture(t *testing.T) (*Node, *Mail, *Presence, *Node, *Mail, *Presence) {
	t.Helper()
	dirA, dirB := t.TempDir(), t.TempDir()
	nodeA, err := StartNode(dirA, nil, []string{"tcp://127.0.0.1:0"}, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nodeA.Stop)
	var peerURL string
	for _, l := range nodeA.listeners {
		peerURL = "tcp://" + l.Addr().String()
	}
	nodeB, err := StartNode(dirB, []string{peerURL}, nil, quietLogger())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nodeB.Stop)
	waitForPeerUp(t, nodeB.Core, 15*time.Second)

	mailA := newTestMail(t, dirA, nodeA)
	mailB := newTestMail(t, dirB, nodeB)
	presenceA := NewPresence(mailA, 300*time.Millisecond)
	presenceB := NewPresence(mailB, 300*time.Millisecond)
	presenceA.Start()
	presenceB.Start()
	if err := mailA.Watch(nodeB.Pub); err != nil {
		t.Fatal(err)
	}
	if err := mailB.Watch(nodeA.Pub); err != nil {
		t.Fatal(err)
	}
	return nodeA, mailA, presenceA, nodeB, mailB, presenceB
}

func findEntry(t *testing.T, entries []presenceEntry, key string) *presenceEntry {
	t.Helper()
	for i := range entries {
		if entries[i].Key == key {
			return &entries[i]
		}
	}
	return nil
}

func TestPresenceOnlineFlow(t *testing.T) {
	nodeA, _, presenceA, nodeB, _, presenceB := presenceFixture(t)

	_ = nodeB
	deadline := time.After(15 * time.Second)
	for {
		entries := presenceA.Snapshot()
		e := findEntry(t, entries, hexString(nodeB.Pub))
		if e != nil && e.Online {
			break
		}
		select {
		case <-deadline:
			t.Fatalf("B never came online at A: %+v", entries)
		case <-time.After(200 * time.Millisecond):
		}
	}

	entries := presenceB.Snapshot()
	e := findEntry(t, entries, hexString(nodeA.Pub))
	if e == nil || !e.Online {
		t.Fatalf("A not online at B: %+v", entries)
	}

	nodeB.Stop()
	offlineDeadline := time.After(20 * time.Second)
	for {
		entries := presenceA.Snapshot()
		e := findEntry(t, entries, hexString(nodeB.Pub))
		if e == nil || !e.Online {
			return
		}
		select {
		case <-offlineDeadline:
			t.Fatalf("B never went offline at A after stop: %+v", entries)
		case <-time.After(500 * time.Millisecond):
		}
	}
}

func TestStatusRoundtrip(t *testing.T) {
	nodeA, mailA, presenceA, nodeB, _, presenceB := presenceFixture(t)

	_ = mailA
	_, err := presenceA.SetStatus(nodeB.Pub, []byte("screen:alice|away:brb"))
	if err != nil {
		t.Fatalf("SetStatus: %v", err)
	}

	want := base64.StdEncoding.EncodeToString([]byte("screen:alice|away:brb"))
	deadline := time.After(15 * time.Second)
	for {
		entries := presenceB.Snapshot()
		e := findEntry(t, entries, hexString(nodeA.Pub))
		if e != nil && e.StatusPayload == want {
			break
		}
		select {
		case <-deadline:
			t.Fatalf("status never arrived at B: %+v", entries)
		case <-time.After(200 * time.Millisecond):
		}
	}

	_, err = presenceA.SetStatus(nodeB.Pub, []byte("screen:alice|away:"))
	if err != nil {
		t.Fatal(err)
	}
	want2 := base64.StdEncoding.EncodeToString([]byte("screen:alice|away:"))
	deadline2 := time.After(15 * time.Second)
	for {
		entries := presenceB.Snapshot()
		e := findEntry(t, entries, hexString(nodeA.Pub))
		if e != nil && e.StatusPayload == want2 {
			return
		}
		select {
		case <-deadline2:
			t.Fatalf("status update never arrived at B: %+v", entries)
		case <-time.After(200 * time.Millisecond):
		}
	}
}

func TestUnknownBuddyNotOnline(t *testing.T) {
	_, _, presenceA, _, _, _ := presenceFixture(t)
	stranger := make(ed25519.PublicKey, 32)
	entries := presenceA.Snapshot()
	if e := findEntry(t, entries, hexString(stranger)); e != nil && e.Online {
		t.Fatal("stranger should not be online")
	}
}
