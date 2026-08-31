package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

func osReadDir(dir string) ([]string, error) {
	files, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var names []string
	for _, f := range files {
		if !f.IsDir() {
			names = append(names, f.Name())
		}
	}
	return names, nil
}

func base64Decode(s string) ([]byte, error) {
	return base64.StdEncoding.DecodeString(s)
}

type Mailbox struct {
	journal *OutboxJournal
	inbox   *InboxStore
}

type Mail struct {
	mu            sync.Mutex
	datadir       string
	inboxCapacity int
	retryInterval time.Duration
	boxes         map[string]*Mailbox
	node          *Node
	probeSeq      uint64

	Presence *Presence

	OnDeliver func(from ed25519.PublicKey, seq uint64, ts int64, payload []byte)
	OnAcked   func(to ed25519.PublicKey, seq uint64)
}

func NewMail(datadir string, inboxCapacity int, retryInterval time.Duration) (*Mail, error) {
	m := &Mail{
		datadir:       datadir,
		inboxCapacity: inboxCapacity,
		retryInterval: retryInterval,
		boxes:         make(map[string]*Mailbox),
	}
	dir := journalDir(datadir)
	entries, err := osReadDir(dir)
	if err != nil {
		return nil, err
	}
	for _, e := range entries {
		name := e
		if len(name) > 6 && name[len(name)-6:] == ".jsonl" {
			peerHex := name[:len(name)-6]
			if _, err := m.boxFor(peerHex); err != nil {
				return nil, err
			}
		}
	}
	contacts, err := m.loadContacts()
	if err != nil {
		return nil, err
	}
	for _, peerHex := range contacts {
		if _, err := m.boxFor(peerHex); err != nil {
			return nil, err
		}
	}
	return m, nil
}

type contactsFile struct {
	Contacts []string `json:"contacts"`
}

func (m *Mail) contactsPath() string {
	return filepath.Join(m.datadir, "contacts.json")
}

func (m *Mail) loadContacts() ([]string, error) {
	data, err := os.ReadFile(m.contactsPath())
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var cf contactsFile
	if err := json.Unmarshal(data, &cf); err != nil {
		return nil, fmt.Errorf("parse %s: %w", m.contactsPath(), err)
	}
	return cf.Contacts, nil
}

func (m *Mail) Watch(pub ed25519.PublicKey) error {
	peerHex := hex.EncodeToString(pub)
	if _, err := m.boxFor(peerHex); err != nil {
		return err
	}
	contacts, err := m.loadContacts()
	if err != nil {
		return err
	}
	for _, c := range contacts {
		if c == peerHex {
			if m.Presence != nil {
				m.Presence.Watch(pub)
			}
			return nil
		}
	}
	contacts = append(contacts, peerHex)
	data, err := json.Marshal(contactsFile{Contacts: contacts})
	if err != nil {
		return err
	}
	tmp := m.contactsPath() + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, m.contactsPath()); err != nil {
		return err
	}
	if m.Presence != nil {
		m.Presence.Watch(pub)
	}
	return nil
}

func (m *Mail) Attach(node *Node) {
	m.node = node
	go m.retryLoop()
}

func (m *Mail) boxFor(peerHex string) (*Mailbox, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if box, ok := m.boxes[peerHex]; ok {
		return box, nil
	}
	journal, err := NewOutboxJournal(m.datadir, peerHex)
	if err != nil {
		return nil, err
	}
	inbox, err := NewInboxStore(m.datadir, peerHex, m.inboxCapacity)
	if err != nil {
		return nil, err
	}
	box := &Mailbox{journal: journal, inbox: inbox}
	m.boxes[peerHex] = box
	return box, nil
}

func (m *Mail) SendMsg(to ed25519.PublicKey, payload []byte) (uint64, error) {
	peerHex := hex.EncodeToString(to)
	box, err := m.boxFor(peerHex)
	if err != nil {
		return 0, err
	}
	seq, err := box.journal.NextSeq()
	if err != nil {
		return 0, err
	}
	env := &Envelope{Version: envelopeVersion, Type: TypeMsg, Seq: seq, Ts: time.Now().UnixMilli(), Payload: payload}
	if err := box.journal.Queue(seq, env.Ts, payload); err != nil {
		return 0, err
	}
	m.flushPub(to)
	return seq, nil
}

func (m *Mail) HandlePacket(from ed25519.PublicKey, payload []byte) {
	env, err := DecodeEnvelope(payload)
	if err != nil {
		return
	}
	if m.Presence != nil {
		m.Presence.Touch(from)
	}
	peerHex := hex.EncodeToString(from)
	box, err := m.boxFor(peerHex)
	if err != nil {
		return
	}
	switch env.Type {
	case TypeMsg:
		isNew, err := box.inbox.Add(env.Seq, env.Ts, env.Payload)
		if err != nil {
			return
		}
		if ackBytes, err := encodeAck(env.Seq); err == nil {
			_, _ = m.node.Send(from, ackBytes)
		}
		if isNew && m.OnDeliver != nil {
			m.OnDeliver(from, env.Seq, env.Ts, env.Payload)
		}
	case TypeAck:
		removed, err := box.journal.Ack(env.Seq)
		if err == nil && removed && m.OnAcked != nil {
			m.OnAcked(from, env.Seq)
		}
	case TypeProbe:
		if m.Presence != nil {
			m.Presence.OnProbe(from, env.Seq)
		}
	case TypeStatus:
		if m.Presence != nil {
			m.Presence.OnStatus(from, env.Seq, env.Ts, env.Payload)
		}
	}
}

func (m *Mail) PathUp(key ed25519.PublicKey) {
	m.flushPub(key)
	if m.Presence != nil {
		m.Presence.PathUp(key)
	}
}

func (m *Mail) Buddies() []ed25519.PublicKey {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]ed25519.PublicKey, 0, len(m.boxes))
	for peerHex := range m.boxes {
		if keyBytes, err := hex.DecodeString(peerHex); err == nil && len(keyBytes) == ed25519.PublicKeySize {
			out = append(out, ed25519.PublicKey(keyBytes))
		}
	}
	return out
}

func (m *Mail) SendProbe(to ed25519.PublicKey) error {
	m.mu.Lock()
	m.probeSeq++
	seq := m.probeSeq
	m.mu.Unlock()
	env := &Envelope{Version: envelopeVersion, Type: TypeProbe, Seq: seq, Ts: time.Now().UnixMilli()}
	data, err := env.Encode()
	if err != nil {
		return err
	}
	_, err = m.node.Send(to, data)
	return err
}

func (m *Mail) SendStatus(to ed25519.PublicKey, seq uint64, payload []byte) error {
	env := &Envelope{Version: envelopeVersion, Type: TypeStatus, Seq: seq, Ts: time.Now().UnixMilli(), Payload: payload}
	data, err := env.Encode()
	if err != nil {
		return err
	}
	_, err = m.node.Send(to, data)
	return err
}

func (m *Mail) flushPub(pub ed25519.PublicKey) {
	m.flushPeer(hex.EncodeToString(pub))
}

func (m *Mail) flushPeer(peerHex string) {
	m.mu.Lock()
	box, ok := m.boxes[peerHex]
	m.mu.Unlock()
	if !ok || m.node == nil {
		return
	}
	pubBytes, err := hex.DecodeString(peerHex)
	if err != nil {
		return
	}
	pub := ed25519.PublicKey(pubBytes)
	for _, entry := range box.journal.Pending() {
		payload, err := base64Decode(entry.Payload)
		if err != nil {
			continue
		}
		env := &Envelope{Version: envelopeVersion, Type: TypeMsg, Seq: entry.Seq, Ts: entry.Ts, Payload: payload}
		if data, err := env.Encode(); err == nil {
			_, _ = m.node.Send(pub, data)
		}
	}
}

func (m *Mail) retryLoop() {
	ticker := time.NewTicker(m.retryInterval)
	defer ticker.Stop()
	for range ticker.C {
		m.mu.Lock()
		peers := make([]string, 0, len(m.boxes))
		for peerHex := range m.boxes {
			peers = append(peers, peerHex)
		}
		m.mu.Unlock()
		for _, peerHex := range peers {
			m.mu.Lock()
			pending := len(m.boxes[peerHex].journal.Pending())
			m.mu.Unlock()
			if pending > 0 {
				m.flushPeer(peerHex)
			}
		}
	}
}

func (m *Mail) History(peerHex string, afterSeq uint64) ([]journalEntry, uint64, uint64, error) {
	box, err := m.boxFor(peerHex)
	if err != nil {
		return nil, 0, 0, err
	}
	msgs := box.inbox.After(afterSeq)
	return msgs, box.inbox.Oldest(), box.inbox.Latest(), nil
}
