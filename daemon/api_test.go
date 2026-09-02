package main

import (
	"bufio"
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"strings"
	"testing"
	"time"
)

type apiClient struct {
	conn net.Conn
	r    *bufio.Reader
}

func dialAPI(t *testing.T, path string) *apiClient {
	t.Helper()
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatalf("dial api: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	return &apiClient{conn: conn, r: bufio.NewReader(conn)}
}

func (c *apiClient) send(t *testing.T, msg apiMessage) {
	t.Helper()
	data, err := json.Marshal(msg)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.conn.Write(append(data, '\n')); err != nil {
		t.Fatalf("write api: %v", err)
	}
}

func (c *apiClient) read(t *testing.T, timeout time.Duration) apiMessage {
	t.Helper()
	c.conn.SetReadDeadline(time.Now().Add(timeout))
	line, err := c.r.ReadString('\n')
	if err != nil {
		t.Fatalf("read api: %v", err)
	}
	var msg apiMessage
	if err := json.Unmarshal([]byte(line), &msg); err != nil {
		t.Fatalf("parse api line %q: %v", line, err)
	}
	return msg
}

func startAPIFixture(t *testing.T) (*Node, *APIServer, *Presence, string) {
	t.Helper()
	dir := t.TempDir()
	node, err := StartNode(dir, nil, nil, quietLogger())
	if err != nil {
		t.Fatalf("start node: %v", err)
	}
	t.Cleanup(node.Stop)
	mail := newTestMail(t, dir, node)
	presence := NewPresence(mail, 250*time.Millisecond)
	presence.Start()
	sock := t.TempDir() + "/api.sock"
	api, err := NewAPIServer(node, mail, presence, sock)
	if err != nil {
		t.Fatalf("start api: %v", err)
	}
	t.Cleanup(func() { api.Close() })
	return node, api, presence, sock
}

func TestAPIWhoami(t *testing.T) {
	node, _, _, sock := startAPIFixture(t)
	client := dialAPI(t, sock)
	client.send(t, apiMessage{Op: "whoami"})
	resp := client.read(t, 5*time.Second)
	if resp.Op != "whoami" {
		t.Fatalf("op = %s, want whoami", resp.Op)
	}
	if resp.Address != node.Address.String() {
		t.Fatalf("address = %s, want %s", resp.Address, node.Address.String())
	}
	if resp.Key != hex.EncodeToString(node.Pub) {
		t.Fatalf("key mismatch")
	}
	if len(resp.Key) != 2*ed25519.PublicKeySize {
		t.Fatalf("key length = %d, want %d", len(resp.Key), 2*ed25519.PublicKeySize)
	}
}

func TestAPIStatus(t *testing.T) {
	_, _, _, sock := startAPIFixture(t)
	client := dialAPI(t, sock)
	client.send(t, apiMessage{Op: "status"})
	resp := client.read(t, 5*time.Second)
	if resp.Op != "status" {
		t.Fatalf("op = %s, want status", resp.Op)
	}
	if resp.Build != buildVersion {
		t.Fatalf("build = %s, want %s", resp.Build, buildVersion)
	}
	if resp.MTU <= 0 {
		t.Fatalf("mtu = %d, want > 0", resp.MTU)
	}
}
func TestAPISendDeliversPacket(t *testing.T) {
	dirA, dirB := t.TempDir(), t.TempDir()
	nodeA, err := StartNode(dirA, nil, []string{"tcp://127.0.0.1:0"}, quietLogger())
	if err != nil {
		t.Fatalf("start node A: %v", err)
	}
	defer nodeA.Stop()
	var peerURL string
	for _, l := range nodeA.listeners {
		peerURL = "tcp://" + l.Addr().String()
	}
	nodeB, err := StartNode(dirB, []string{peerURL}, nil, quietLogger())
	if err != nil {
		t.Fatalf("start node B: %v", err)
	}
	defer nodeB.Stop()
	waitForPeerUp(t, nodeB.Core, 15*time.Second)

	mailA := newTestMail(t, dirA, nodeA)
	mailB := newTestMail(t, dirB, nodeB)
	delivered := make(chan struct{}, 8)
	var gotFrom ed25519.PublicKey
	var gotPayload []byte
	mailB.OnDeliver = func(from ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
		gotFrom = from
		gotPayload = payload
		delivered <- struct{}{}
	}

	sock := t.TempDir() + "/api.sock"
	presenceA := NewPresence(mailA, 250*time.Millisecond)
	presenceA.Start()
	api, err := NewAPIServer(nodeA, mailA, presenceA, sock)
	if err != nil {
		t.Fatalf("start api: %v", err)
	}
	defer api.Close()
	mailA.OnDeliver = api.DeliverMsg
	mailA.OnAcked = api.Acked

	client := dialAPI(t, sock)
	client.send(t, apiMessage{
		Op:      "send",
		To:      hex.EncodeToString(nodeB.Pub),
		Payload: base64.StdEncoding.EncodeToString([]byte("hello b")),
	})
	resp := client.read(t, 5*time.Second)
	if resp.Op != "queued" {
		t.Fatalf("op = %s (%s), want queued", resp.Op, resp.Error)
	}
	if resp.Seq != 1 {
		t.Fatalf("seq = %d, want 1", resp.Seq)
	}

	select {
	case <-delivered:
	case <-time.After(20 * time.Second):
		t.Fatal("no delivery within 20s")
	}
	if string(gotPayload) != "hello b" {
		t.Fatalf("B got %q, want hello b", gotPayload)
	}
	if !gotFrom.Equal(nodeA.Pub) {
		t.Fatal("sender mismatch")
	}

	if _, err := mailB.SendMsg(nodeA.Pub, []byte("hello a")); err != nil {
		t.Fatalf("B reply: %v", err)
	}

	var recvMsg apiMessage
	for {
		msg := client.read(t, 20*time.Second)
		if msg.Op == "recv" {
			recvMsg = msg
			break
		}
	}
	if recvMsg.From != hex.EncodeToString(nodeB.Pub) {
		t.Fatalf("from = %s, want %s", recvMsg.From, hex.EncodeToString(nodeB.Pub))
	}
	payload, err := base64.StdEncoding.DecodeString(recvMsg.Payload)
	if err != nil {
		t.Fatalf("decode payload: %v", err)
	}
	if string(payload) != "hello a" {
		t.Fatalf("payload = %q, want hello a", payload)
	}
	if recvMsg.Seq != 1 {
		t.Fatalf("recv seq = %d, want 1", recvMsg.Seq)
	}
}

func TestAPISendErrors(t *testing.T) {
	_, _, _, sock := startAPIFixture(t)
	client := dialAPI(t, sock)

	cases := []struct {
		name    string
		to      string
		payload string
		frag    string
	}{
		{"bad hex", "zzzz", "aGk=", "bad key hex"},
		{"short key", "abcd", "aGk=", "hex chars"},
		{"bad base64", hex.EncodeToString(make([]byte, 32)), "!!!", "bad payload base64"},
	}
	for _, tc := range cases {
		client.send(t, apiMessage{Op: "send", To: tc.to, Payload: tc.payload})
		resp := client.read(t, 5*time.Second)
		if resp.Op != "error" {
			t.Fatalf("%s: op = %s, want error", tc.name, resp.Op)
		}
		if !strings.Contains(resp.Error, tc.frag) {
			t.Fatalf("%s: error = %q, want contains %q", tc.name, resp.Error, tc.frag)
		}
	}
}

func TestAPISurvivesGarbage(t *testing.T) {
	_, _, _, sock := startAPIFixture(t)
	conn, err := net.Dial("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	garbage := bytes.Repeat([]byte{0xFF, 0x00, 0x41}, 1000)
	conn.Write(garbage)
	conn.Write([]byte("\n"))
	conn.Write([]byte{0x00, 0x01, 0x02})
	conn.Close()
	time.Sleep(200 * time.Millisecond)

	conn2, err := net.Dial("unix", sock)
	if err != nil {
		t.Fatalf("daemon stopped accepting after garbage: %v", err)
	}
	fmt.Fprintf(conn2, "{\"op\":\"whoami\"}\n")
	buf := make([]byte, 1024)
	conn2.SetReadDeadline(time.Now().Add(5 * time.Second))
	n, err := conn2.Read(buf)
	if err != nil || !bytes.Contains(buf[:n], []byte("whoami")) {
		t.Fatalf("daemon unresponsive after garbage: n=%d err=%v", n, err)
	}
	conn2.Close()
}
