package main

import (
	"crypto/ed25519"
	"crypto/x509"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/yggdrasil-network/yggdrasil-go/src/core"
)

func wireMail(node *Node, m *Mail) {
	node.OnPacket = m.HandlePacket
	node.OnPathUp = m.PathUp
}

func hexString(b []byte) string {
	return hex.EncodeToString(b)
}

func TestLoadOrCreateKey(t *testing.T) {
	dir := t.TempDir()
	priv1, err := LoadOrCreateKey(dir)
	if err != nil {
		t.Fatalf("first load: %v", err)
	}
	priv2, err := LoadOrCreateKey(dir)
	if err != nil {
		t.Fatalf("second load: %v", err)
	}
	if !ed25519.PrivateKey(priv1).Equal(priv2) {
		t.Fatal("keys differ between loads")
	}
	info, err := os.Stat(filepath.Join(dir, keyFileName))
	if err != nil {
		t.Fatalf("stat key file: %v", err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("key file perms = %v, want 0600", info.Mode().Perm())
	}
}

func TestLoadOrCreateKeyRejectsCorruptFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, keyFileName)
	if err := os.WriteFile(path, []byte("not-hex!"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadOrCreateKey(dir); err == nil {
		t.Fatal("expected error for corrupt key file")
	}
}

func TestSelfSignedCert(t *testing.T) {
	dir := t.TempDir()
	priv, err := LoadOrCreateKey(dir)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := selfSignedCert(priv)
	if err != nil {
		t.Fatalf("selfSignedCert: %v", err)
	}
	if len(cert.Certificate) != 1 {
		t.Fatalf("cert chain len = %d, want 1", len(cert.Certificate))
	}
	leaf, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		t.Fatalf("parse leaf: %v", err)
	}
	wantCN := hex.EncodeToString(priv.Public().(ed25519.PublicKey))
	if leaf.Subject.CommonName != wantCN {
		t.Fatalf("CN = %s, want %s", leaf.Subject.CommonName, wantCN)
	}
	if cert.PrivateKey.(ed25519.PrivateKey).Equal(priv) != true {
		t.Fatal("cert private key mismatch")
	}
}

func TestTwoNodeLoopbackEcho(t *testing.T) {
	dirA := t.TempDir()
	dirB := t.TempDir()

	nodeA, err := StartNode(dirA, nil, []string{"tcp://127.0.0.1:0"}, quietLogger())
	if err != nil {
		t.Fatalf("start node A: %v", err)
	}
	defer nodeA.Stop()

	var peerURL string
	for _, l := range nodeA.listeners {
		peerURL = "tcp://" + l.Addr().String()
	}
	if peerURL == "" {
		t.Fatal("node A has no listener")
	}

	nodeB, err := StartNode(dirB, []string{peerURL}, nil, quietLogger())
	if err != nil {
		t.Fatalf("start node B: %v", err)
	}
	defer nodeB.Stop()

	waitForPeerUp(t, nodeB.Core, 15*time.Second)

	pktA := make(chan packet, 8)
	pktB := make(chan packet, 8)
	nodeA.OnPacket = func(from ed25519.PublicKey, payload []byte) {
		pktA <- packet{from: from, payload: payload}
	}
	nodeB.OnPacket = func(from ed25519.PublicKey, payload []byte) {
		pktB <- packet{from: from, payload: payload}
	}

	got := sendUntilReceived(t, nodeB, nodeA.Pub, []byte("ping"), pktA, 30*time.Second)
	if string(got.payload) != "ping" {
		t.Fatalf("A got %q, want ping", got.payload)
	}
	if !got.from.Equal(nodeB.Pub) {
		t.Fatalf("A sender = %x, want %x", got.from, nodeB.Pub)
	}

	got = sendUntilReceived(t, nodeA, nodeB.Pub, []byte("pong"), pktB, 30*time.Second)
	if string(got.payload) != "pong" {
		t.Fatalf("B got %q, want pong", got.payload)
	}
	if !got.from.Equal(nodeA.Pub) {
		t.Fatalf("B sender = %x, want %x", got.from, nodeA.Pub)
	}
}

func TestSendRejectsBadKey(t *testing.T) {
	dir := t.TempDir()
	node, err := StartNode(dir, nil, nil, quietLogger())
	if err != nil {
		t.Fatalf("start node: %v", err)
	}
	defer node.Stop()
	if _, err := node.Send(ed25519.PublicKey("short"), []byte("x")); err == nil {
		t.Fatal("expected error for short key")
	}
	big := make([]byte, int(node.Core.MTU())+1)
	if _, err := node.Send(node.Pub, big); err == nil {
		t.Fatal("expected error for oversized payload")
	}
}

type packet struct {
	from    ed25519.PublicKey
	payload []byte
}

func quietLogger() *aimlessLogger {
	return &aimlessLogger{verbose: false}
}

func waitForPeerUp(t *testing.T, c *core.Core, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		for _, p := range c.GetPeers() {
			if p.Up {
				return
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("no peer came up within %s", timeout)
}

func recvPacket(t *testing.T, ch chan packet, timeout time.Duration) packet {
	t.Helper()
	if timeout <= 0 {
		return <-ch
	}
	select {
	case p := <-ch:
		return p
	case <-time.After(timeout):
		t.Fatalf("no packet within %s", timeout)
		return packet{}
	}
}

func sendUntilReceived(t *testing.T, from *Node, to ed25519.PublicKey, payload []byte, dst chan packet, timeout time.Duration) packet {
	t.Helper()
	deadline := time.After(timeout)
	for {
		if _, err := from.Send(to, payload); err != nil {
			t.Fatalf("send: %v", err)
		}
		select {
		case p := <-dst:
			return p
		case <-time.After(250 * time.Millisecond):
		case <-deadline:
			t.Fatalf("no delivery within %s", timeout)
		}
	}
}

// The writeFn seam + a bare &Node{} let these tests run without a real core.

func TestOutboundControlPriority(t *testing.T) {
	pub := make(ed25519.PublicKey, 32)
	n := &Node{
		control: make(chan outbound, 64),
		bulk:    make(chan outbound, 512),
	}
	var mu sync.Mutex
	var order []string
	bulkStarted := make(chan struct{}, 1)
	release := make(chan struct{})
	n.writeFn = func(_ ed25519.PublicKey, payload []byte) {
		mu.Lock()
		order = append(order, string(payload))
		mu.Unlock()
		if string(payload) == "bulk-1" {
			select {
			case bulkStarted <- struct{}{}:
			default:
			}
			<-release // hold bulk-1 in flight so the backlog is real
		}
	}
	go n.writeLoop()

	for i := 1; i <= 5; i++ {
		if _, err := n.SendBulk(pub, []byte(fmt.Sprintf("bulk-%d", i))); err != nil {
			t.Fatalf("bulk %d: %v", i, err)
		}
	}
	<-bulkStarted // bulk-1 is mid-write; the writer cannot reach the select
	if _, err := n.Send(pub, []byte("control")); err != nil {
		t.Fatal(err)
	}
	close(release)

	deadline := time.Now().Add(5 * time.Second)
	for {
		mu.Lock()
		done := len(order) == 6
		mu.Unlock()
		if done || time.Now().After(deadline) {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	want := []string{"bulk-1", "control", "bulk-2", "bulk-3", "bulk-4", "bulk-5"}
	if !reflect.DeepEqual(order, want) {
		t.Fatalf("write order = %v, want %v (control must jump the bulk backlog)", order, want)
	}
}

func TestBulkQueueFullLeavesControlHeadroom(t *testing.T) {
	pub := make(ed25519.PublicKey, 32)
	n := &Node{
		control: make(chan outbound, 64),
		bulk:    make(chan outbound, 2),
		writeFn: func(_ ed25519.PublicKey, _ []byte) {},
	}
	for i := 0; i < 2; i++ {
		if _, err := n.SendBulk(pub, []byte("x")); err != nil {
			t.Fatalf("bulk %d: %v", i, err)
		}
	}
	if _, err := n.SendBulk(pub, []byte("y")); err == nil {
		t.Fatal("full bulk queue must drop with an error")
	}
	if _, err := n.Send(pub, []byte("control")); err != nil {
		t.Fatalf("control enqueue must be independent of a full bulk queue: %v", err)
	}
}
