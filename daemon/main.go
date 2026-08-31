package main

import (
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func acquireLock(datadir string) error {
	if err := os.MkdirAll(datadir, 0o700); err != nil {
		return fmt.Errorf("create datadir: %w", err)
	}
	path := filepath.Join(datadir, "lock")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return fmt.Errorf("open lock file: %w", err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return fmt.Errorf("another aimlessd instance is already using %s", datadir)
	}
	return nil
}

var defaultPeers = []string{
	"tls://london.sabretruth.org:18472",
	"tls://yggdrasil.neilalexander.dev:64648?key=ecbbcb3298e7d3b4196103333c3e839cfe47a6ca47602b94a6d596683f6bb358",
}

const defaultInboxCapacity = 50

const defaultRetryInterval = 2 * time.Second

const defaultProbeInterval = 15 * time.Second

type configFile struct {
	Peers  []string `json:"peers"`
	Listen []string `json:"listen"`
}

func loadConfig(path string) (*configFile, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return &configFile{}, nil
	}
	if err != nil {
		return nil, err
	}
	var cfg configFile
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	return &cfg, nil
}

func main() {
	log.SetFlags(log.LstdFlags)

	home, _ := os.UserHomeDir()
	defaultDatadir := filepath.Join(home, ".local", "share", "aimless")

	datadir := flag.String("datadir", defaultDatadir, "data directory for node key and config")
	apiPath := flag.String("api", "", "unix socket path (default: <datadir>/api.sock)")
	peersFlag := flag.String("peers", "", "comma-separated peer URIs, or 'none' (overrides config)")
	listenFlag := flag.String("listen", "", "comma-separated link listener URIs")
	retryFlag := flag.Duration("retry", defaultRetryInterval, "outbox retry interval")
	probeFlag := flag.Duration("probe", defaultProbeInterval, "presence probe interval")
	verbose := flag.Bool("verbose", false, "debug logging")
	flag.Parse()

	if err := run(*datadir, *apiPath, *peersFlag, *listenFlag, *retryFlag, *probeFlag, *verbose); err != nil {
		log.Fatalf("aimlessd: %v", err)
	}
}

func run(datadir, apiPath, peersFlag, listenFlag string, retryInterval, probeInterval time.Duration, verbose bool) error {
	logger := &aimlessLogger{verbose: verbose}

	if err := acquireLock(datadir); err != nil {
		return err
	}

	cfg, err := loadConfig(filepath.Join(datadir, "config.json"))
	if err != nil {
		return err
	}
	peers := cfg.Peers
	if peersFlag != "" {
		if peersFlag == "none" {
			peers = nil
		} else {
			peers = strings.Split(peersFlag, ",")
		}
	}
	if len(peers) == 0 && peersFlag != "none" {
		peers = defaultPeers
	}
	listenURLs := cfg.Listen
	if listenFlag != "" {
		listenURLs = strings.Split(listenFlag, ",")
	}

	node, err := StartNode(datadir, peers, listenURLs, logger)
	if err != nil {
		return err
	}
	defer node.Stop()

	mail, err := NewMail(datadir, defaultInboxCapacity, retryInterval)
	if err != nil {
		return err
	}
	node.OnPacket = mail.HandlePacket
	node.OnPathUp = mail.PathUp
	mail.Attach(node)

	presence := NewPresence(mail, probeInterval)
	presence.Start()

	if apiPath == "" {
		apiPath = filepath.Join(datadir, "api.sock")
	}
	api, err := NewAPIServer(node, mail, presence, apiPath)
	if err != nil {
		return err
	}
	defer api.Close()
	mail.OnDeliver = api.DeliverMsg
	mail.OnAcked = api.Acked

	fmt.Printf("aimlessd %s\n", buildVersion)
	fmt.Printf("  address: %s\n", node.Address.String())
	fmt.Printf("  pubkey:  %s\n", hex.EncodeToString(node.Pub))
	fmt.Printf("  api:     %s\n", apiPath)
	for _, p := range peers {
		fmt.Printf("  peer:    %s\n", p)
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig
	fmt.Println("shutting down")
	return nil
}
