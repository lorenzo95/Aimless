package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"sort"
	"sync"
	"time"
)

type statusOut struct {
	seq     uint64
	payload []byte
}

type statusIn struct {
	seq     uint64
	ts      int64
	payload []byte
}

type peerPresence struct {
	lastSeen time.Time
	in       *statusIn
	out      *statusOut
	// detached is a client-supplied, pre-sealed status blob shown to the peer
	// while NO API client is attached (the always-on-daemon / GUI-off case).
	// Opaque like `out` — the daemon only relays it, never reads it.
	detached *statusOut
}

type Presence struct {
	mu            sync.Mutex
	mail          *Mail
	probeInterval time.Duration
	onlineWindow  time.Duration
	peers         map[string]*peerPresence
	attached      bool
}

func NewPresence(mail *Mail, probeInterval time.Duration) *Presence {
	p := &Presence{
		mail:          mail,
		probeInterval: probeInterval,
		onlineWindow:  3 * probeInterval,
		peers:         make(map[string]*peerPresence),
	}
	mail.Presence = p
	return p
}

func (p *Presence) Start() {
	go p.loop()
}

func (p *Presence) loop() {
	ticker := time.NewTicker(p.probeInterval)
	defer ticker.Stop()
	for range ticker.C {
		p.tick()
	}
}

func (p *Presence) keyFor(pub ed25519.PublicKey) *peerPresence {
	peerHex := hex.EncodeToString(pub)
	pp, ok := p.peers[peerHex]
	if !ok {
		pp = &peerPresence{}
		p.peers[peerHex] = pp
	}
	return pp
}

func (p *Presence) Watch(pub ed25519.PublicKey) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.keyFor(pub)
}

func (p *Presence) Touch(pub ed25519.PublicKey) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.keyFor(pub).lastSeen = time.Now()
}

func (p *Presence) OnProbe(pub ed25519.PublicKey, seq uint64) {
	p.Touch(pub)
}

func (p *Presence) OnStatus(pub ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	pp := p.keyFor(pub)
	if pp.in == nil || ts > pp.in.ts {
		pp.in = &statusIn{seq: seq, ts: ts, payload: payload}
	}
}

func (p *Presence) SetStatus(pub ed25519.PublicKey, payload []byte) (uint64, error) {
	p.mu.Lock()
	pp := p.keyFor(pub)
	if pp.out == nil {
		pp.out = &statusOut{}
	}
	pp.out.seq++
	seq := pp.out.seq
	pp.out.payload = payload
	p.mu.Unlock()
	err := p.mail.SendStatus(pub, seq, payload)
	return seq, err
}

// SetDetached stores the client's pre-sealed 'away - client offline' blob for
// one peer. It is relayed instead of the live status while no API client is
// attached, so buddies see the offline message on the always-on daemon.
func (p *Presence) SetDetached(pub ed25519.PublicKey, payload []byte) {
	p.mu.Lock()
	pp := p.keyFor(pub)
	if pp.detached == nil {
		pp.detached = &statusOut{}
	}
	pp.detached.seq++
	pp.detached.payload = payload
	p.mu.Unlock()
}

// SetAttached flips the client-attached state (called by the API server when
// the first client connects / the last one disconnects) and immediately pushes
// the appropriate blob so peers see the change without waiting for a probe tick.
func (p *Presence) SetAttached(attached bool) {
	p.mu.Lock()
	p.attached = attached
	targets := make([]ed25519.PublicKey, 0, len(p.peers))
	for peerHex := range p.peers {
		if keyBytes, err := hex.DecodeString(peerHex); err == nil && len(keyBytes) == ed25519.PublicKeySize {
			targets = append(targets, ed25519.PublicKey(keyBytes))
		}
	}
	p.mu.Unlock()
	for _, pub := range targets {
		if p.mail.IsBlocked(pub) {
			continue
		}
		p.mu.Lock()
		pp := p.peers[hex.EncodeToString(pub)]
		st := p.blobFor(pp)
		p.mu.Unlock()
		if st != nil {
			_ = p.mail.SendStatus(pub, st.seq, st.payload)
		}
	}
}

// blobFor returns the status blob to relay for a peer: the detached offline
// blob while unattached (falling back to the live one if none was supplied),
// otherwise the live status.
func (p *Presence) blobFor(pp *peerPresence) *statusOut {
	if pp == nil {
		return nil
	}
	if !p.attached && pp.detached != nil {
		return pp.detached
	}
	return pp.out
}

func (p *Presence) PathUp(pub ed25519.PublicKey) {
	p.probeOne(pub)
}

func (p *Presence) tick() {
	p.mu.Lock()
	targets := make([]ed25519.PublicKey, 0, len(p.peers))
	for peerHex := range p.peers {
		if keyBytes, err := hex.DecodeString(peerHex); err == nil && len(keyBytes) == ed25519.PublicKeySize {
			targets = append(targets, ed25519.PublicKey(keyBytes))
		}
	}
	p.mu.Unlock()
	for _, pub := range p.mail.Buddies() {
		peerHex := hex.EncodeToString(pub)
		p.mu.Lock()
		_, known := p.peers[peerHex]
		p.mu.Unlock()
		if !known {
			targets = append(targets, pub)
		}
	}
	for _, pub := range targets {
		p.probeOne(pub)
	}
}

func (p *Presence) probeOne(pub ed25519.PublicKey) {
	if p.mail.IsBlocked(pub) {
		return
	}
	_ = p.mail.SendProbe(pub)
	p.mu.Lock()
	pp := p.peers[hex.EncodeToString(pub)]
	st := p.blobFor(pp)
	p.mu.Unlock()
	if st != nil {
		_ = p.mail.SendStatus(pub, st.seq, st.payload)
	}
}

type presenceEntry struct {
	Key           string `json:"key"`
	Online        bool   `json:"online"`
	StatusTs      int64  `json:"status_ts,omitempty"`
	StatusPayload string `json:"status_payload,omitempty"`
}

func (p *Presence) Snapshot() []presenceEntry {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]presenceEntry, 0, len(p.peers))
	now := time.Now()
	for peerHex, pp := range p.peers {
		if p.mail.IsBlockedHex(peerHex) {
			continue
		}
		e := presenceEntry{Key: peerHex, Online: now.Sub(pp.lastSeen) < p.onlineWindow}
		if pp.in != nil {
			e.StatusTs = pp.in.ts
			e.StatusPayload = base64.StdEncoding.EncodeToString(pp.in.payload)
		}
		out = append(out, e)
	}
	sort.Slice(out, func(i, k int) bool { return out[i].Key < out[k].Key })
	return out
}
