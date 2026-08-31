package main

import (
	"crypto/ed25519"
	"crypto/x509"
	"encoding/hex"
	"os"
	"path/filepath"
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
