package main

import (
	"bufio"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"sync"
)

const buildVersion = "aimlessd/0.2.2"

type peerStatus struct {
	URI     string `json:"uri"`
	Up      bool   `json:"up"`
	Latency int64  `json:"latency_ms"`
	Inbound bool   `json:"inbound"`
	TXBytes uint64 `json:"tx_bytes"`
	RXBytes uint64 `json:"rx_bytes"`
}

type historyMsg struct {
	Seq     uint64 `json:"seq"`
	Ts      int64  `json:"ts"`
	Payload string `json:"payload"`
}

type apiMessage struct {
	Op       string          `json:"op"`
	Address  string          `json:"address,omitempty"`
	Key      string          `json:"key,omitempty"`
	To       string          `json:"to,omitempty"`
	From     string          `json:"from,omitempty"`
	Payload  string          `json:"payload,omitempty"`
	Bytes    int             `json:"bytes,omitempty"`
	Seq      uint64          `json:"seq,omitempty"`
	Ts       int64           `json:"ts,omitempty"`
	Error    string          `json:"error,omitempty"`
	Peers    []peerStatus    `json:"peers,omitempty"`
	MTU      int             `json:"mtu,omitempty"`
	Build    string          `json:"build,omitempty"`
	Msgs     []historyMsg    `json:"msgs,omitempty"`
	Oldest   uint64          `json:"oldest,omitempty"`
	Latest   uint64          `json:"latest,omitempty"`
	Presence []presenceEntry `json:"presence,omitempty"`
}

type APIServer struct {
	node     *Node
	mail     *Mail
	presence *Presence
	ln       net.Listener
	mu       sync.Mutex
	conns    map[net.Conn]struct{}
	closed   bool
}

func NewAPIServer(node *Node, mail *Mail, presence *Presence, socketPath string) (*APIServer, error) {
	if err := os.Remove(socketPath); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("remove stale socket: %w", err)
	}
	ln, err := net.Listen("unix", socketPath)
	if err != nil {
		return nil, fmt.Errorf("listen on %s: %w", socketPath, err)
	}
	if err := os.Chmod(socketPath, 0o600); err != nil {
		ln.Close()
		return nil, fmt.Errorf("chmod socket: %w", err)
	}
	s := &APIServer{node: node, mail: mail, presence: presence, ln: ln, conns: make(map[net.Conn]struct{})}
	go s.acceptLoop()
	return s, nil
}

func (s *APIServer) acceptLoop() {
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}
		s.mu.Lock()
		if s.closed {
			conn.Close()
			s.mu.Unlock()
			return
		}
		s.conns[conn] = struct{}{}
		s.mu.Unlock()
		go s.handleConn(conn)
	}
}

func (s *APIServer) handleConn(conn net.Conn) {
	defer func() {
		s.mu.Lock()
		delete(s.conns, conn)
		s.mu.Unlock()
		conn.Close()
	}()
	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var req apiMessage
		if err := json.Unmarshal(line, &req); err != nil {
			s.reply(conn, apiMessage{Op: "error", Error: "bad json: " + err.Error()})
			continue
		}
		s.dispatch(conn, req)
	}
}

func (s *APIServer) dispatch(conn net.Conn, req apiMessage) {
	switch req.Op {
	case "whoami":
		s.reply(conn, apiMessage{Op: "whoami", Address: s.node.Address.String(), Key: hex.EncodeToString(s.node.Pub)})
	case "status":
		peers := s.node.Core.GetPeers()
		out := make([]peerStatus, 0, len(peers))
		for _, p := range peers {
			out = append(out, peerStatus{
				URI:     p.URI,
				Up:      p.Up,
				Latency: p.Latency.Milliseconds(),
				Inbound: p.Inbound,
				TXBytes: p.TXBytes,
				RXBytes: p.RXBytes,
			})
		}
		s.reply(conn, apiMessage{
			Op:      "status",
			Address: s.node.Address.String(),
			Key:     hex.EncodeToString(s.node.Pub),
			Peers:   out,
			MTU:     int(s.node.Core.MTU()),
			Build:   buildVersion,
		})
	case "send":
		s.handleSend(conn, req)
	case "history":
		s.handleHistory(conn, req)
	case "presence":
		s.reply(conn, apiMessage{Op: "presence", Presence: s.presence.Snapshot()})
	case "watch":
		s.handleWatch(conn, req)
	case "setstatus":
		s.handleSetStatus(conn, req)
	case "":
		s.reply(conn, apiMessage{Op: "error", Error: "missing op"})
	default:
		s.reply(conn, apiMessage{Op: "error", Error: "unknown op: " + req.Op})
	}
}

func (s *APIServer) handleSend(conn net.Conn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.To)
	if err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "bad key hex: " + err.Error()})
		return
	}
	if len(keyBytes) != ed25519.PublicKeySize {
		s.reply(conn, apiMessage{Op: "error", Error: fmt.Sprintf("key must be %d hex chars", 2*ed25519.PublicKeySize)})
		return
	}
	payload, err := base64.StdEncoding.DecodeString(req.Payload)
	if err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "bad payload base64: " + err.Error()})
		return
	}
	seq, err := s.mail.SendMsg(ed25519.PublicKey(keyBytes), payload)
	if err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "send failed: " + err.Error()})
		return
	}
	s.reply(conn, apiMessage{Op: "queued", To: req.To, Seq: seq, Bytes: len(payload)})
}

func (s *APIServer) handleHistory(conn net.Conn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.From)
	if err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "bad key hex: " + err.Error()})
		return
	}
	if len(keyBytes) != ed25519.PublicKeySize {
		s.reply(conn, apiMessage{Op: "error", Error: fmt.Sprintf("key must be %d hex chars", 2*ed25519.PublicKeySize)})
		return
	}
	msgs, oldest, latest, err := s.mail.History(hex.EncodeToString(keyBytes), req.Seq)
	if err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "history failed: " + err.Error()})
		return
	}
	out := make([]historyMsg, 0, len(msgs))
	for _, e := range msgs {
		out = append(out, historyMsg{Seq: e.Seq, Ts: e.Ts, Payload: e.Payload})
	}
	s.reply(conn, apiMessage{Op: "history", From: req.From, Msgs: out, Oldest: oldest, Latest: latest})
}

func (s *APIServer) handleWatch(conn net.Conn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.To)
	if err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "bad key hex: " + err.Error()})
		return
	}
	if len(keyBytes) != ed25519.PublicKeySize {
		s.reply(conn, apiMessage{Op: "error", Error: fmt.Sprintf("key must be %d hex chars", 2*ed25519.PublicKeySize)})
		return
	}
	if err := s.mail.Watch(ed25519.PublicKey(keyBytes)); err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "watch failed: " + err.Error()})
		return
	}
	s.reply(conn, apiMessage{Op: "watching", To: req.To})
}

func (s *APIServer) handleSetStatus(conn net.Conn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.To)
	if err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "bad key hex: " + err.Error()})
		return
	}
	if len(keyBytes) != ed25519.PublicKeySize {
		s.reply(conn, apiMessage{Op: "error", Error: fmt.Sprintf("key must be %d hex chars", 2*ed25519.PublicKeySize)})
		return
	}
	payload, err := base64.StdEncoding.DecodeString(req.Payload)
	if err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "bad payload base64: " + err.Error()})
		return
	}
	seq, err := s.presence.SetStatus(ed25519.PublicKey(keyBytes), payload)
	if err != nil {
		s.reply(conn, apiMessage{Op: "error", Error: "setstatus failed: " + err.Error()})
		return
	}
	s.reply(conn, apiMessage{Op: "statusset", To: req.To, Seq: seq})
}

func (s *APIServer) reply(conn net.Conn, msg apiMessage) {
	data, err := json.Marshal(msg)
	if err != nil {
		return
	}
	conn.Write(append(data, '\n'))
}

func (s *APIServer) DeliverMsg(from ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
	s.broadcast(apiMessage{
		Op:      "recv",
		From:    hex.EncodeToString(from),
		Seq:     seq,
		Ts:      ts,
		Payload: base64.StdEncoding.EncodeToString(payload),
	})
}

func (s *APIServer) Acked(to ed25519.PublicKey, seq uint64) {
	s.broadcast(apiMessage{
		Op:  "acked",
		To:  hex.EncodeToString(to),
		Seq: seq,
	})
}

func (s *APIServer) broadcast(msg apiMessage) {
	data, err := json.Marshal(msg)
	if err != nil {
		return
	}
	data = append(data, '\n')
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return
	}
	for conn := range s.conns {
		conn.Write(data)
	}
}

func (s *APIServer) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	conns := make([]net.Conn, 0, len(s.conns))
	for conn := range s.conns {
		conns = append(conns, conn)
	}
	s.conns = make(map[net.Conn]struct{})
	s.mu.Unlock()
	for _, conn := range conns {
		conn.Close()
	}
	return s.ln.Close()
}
