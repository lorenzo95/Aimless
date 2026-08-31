package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"errors"
	"fmt"
	"math/big"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/Arceliar/ironwood/types"
	"github.com/yggdrasil-network/yggdrasil-go/src/core"
)

const keyFileName = "node.key"

type Node struct {
	Core     *core.Core
	Pub      ed25519.PublicKey
	Address  net.IP
	OnPacket func(from ed25519.PublicKey, payload []byte)
	OnPathUp func(key ed25519.PublicKey)

	priv      ed25519.PrivateKey
	listeners []*core.Listener
	closeOnce sync.Once
}

func LoadOrCreateKey(datadir string) (ed25519.PrivateKey, error) {
	if err := os.MkdirAll(datadir, 0o700); err != nil {
		return nil, fmt.Errorf("create datadir: %w", err)
	}
	path := filepath.Join(datadir, keyFileName)
	data, err := os.ReadFile(path)
	if err == nil {
		seed, decErr := hex.DecodeString(strings.TrimSpace(string(data)))
		if decErr != nil {
			return nil, fmt.Errorf("decode key file: %w", decErr)
		}
		if len(seed) != ed25519.SeedSize {
			return nil, fmt.Errorf("key file has wrong size: %d", len(seed))
		}
		return ed25519.NewKeyFromSeed(seed), nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("read key file: %w", err)
	}
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generate key: %w", err)
	}
	if err := os.WriteFile(path, []byte(hex.EncodeToString(priv.Seed())+"\n"), 0o600); err != nil {
		return nil, fmt.Errorf("write key file: %w", err)
	}
	return priv, nil
}

func selfSignedCert(priv ed25519.PrivateKey) (*tls.Certificate, error) {
	pub, ok := priv.Public().(ed25519.PublicKey)
	if !ok {
		return nil, errors.New("not an ed25519 key")
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: hex.EncodeToString(pub)},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().AddDate(100, 0, 0),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, pub, priv)
	if err != nil {
		return nil, fmt.Errorf("create certificate: %w", err)
	}
	return &tls.Certificate{Certificate: [][]byte{der}, PrivateKey: priv}, nil
}

func StartNode(datadir string, peers []string, listenURLs []string, log *aimlessLogger) (*Node, error) {
	priv, err := LoadOrCreateKey(datadir)
	if err != nil {
		return nil, err
	}
	cert, err := selfSignedCert(priv)
	if err != nil {
		return nil, err
	}
	c, err := core.New(cert, log)
	if err != nil {
		return nil, fmt.Errorf("start yggdrasil core: %w", err)
	}
	n := &Node{
		Core: c,
		priv: priv,
		Pub:  priv.Public().(ed25519.PublicKey),
	}
	n.Address = c.Address()
	c.SetPathNotify(func(key ed25519.PublicKey) {
		if n.OnPathUp != nil {
			n.OnPathUp(key)
		}
	})

	for _, raw := range listenURLs {
		u, err := url.Parse(raw)
		if err != nil {
			n.Stop()
			return nil, fmt.Errorf("parse listen url %q: %w", raw, err)
		}
		l, err := c.Listen(u, "")
		if err != nil {
			n.Stop()
			return nil, fmt.Errorf("listen on %q: %w", raw, err)
		}
		n.listeners = append(n.listeners, l)
	}

	for _, raw := range peers {
		u, err := url.Parse(raw)
		if err != nil {
			n.Stop()
			return nil, fmt.Errorf("parse peer url %q: %w", raw, err)
		}
		if err := c.AddPeer(u, ""); err != nil {
			n.Stop()
			return nil, fmt.Errorf("add peer %q: %w", raw, err)
		}
	}

	go n.readLoop()
	return n, nil
}

func (n *Node) readLoop() {
	mtu := int(n.Core.MTU())
	buf := make([]byte, mtu)
	for {
		nRead, from, err := n.Core.ReadFrom(buf)
		if err != nil {
			return
		}
		if n.OnPacket == nil || nRead == 0 {
			continue
		}
		fromAddr, ok := from.(types.Addr)
		if !ok {
			continue
		}
		payload := make([]byte, nRead)
		copy(payload, buf[:nRead])
		sender := make(ed25519.PublicKey, len(fromAddr))
		copy(sender, fromAddr)
		n.OnPacket(sender, payload)
	}
}

func (n *Node) Send(pub ed25519.PublicKey, payload []byte) (int, error) {
	if len(pub) != ed25519.PublicKeySize {
		return 0, fmt.Errorf("bad public key size: %d", len(pub))
	}
	if len(payload) > int(n.Core.MTU()) {
		return 0, fmt.Errorf("payload %d exceeds mtu %d", len(payload), n.Core.MTU())
	}
	return n.Core.WriteTo(payload, types.Addr(pub))
}

func (n *Node) Stop() {
	n.closeOnce.Do(func() {
		n.Core.Stop()
	})
}
