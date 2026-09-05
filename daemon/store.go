package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

type journalEntry struct {
	Seq     uint64 `json:"seq"`
	Ts      int64  `json:"ts"`
	Payload string `json:"payload"`
}

type OutboxJournal struct {
	mu      sync.Mutex
	path    string
	seqPath string
	entries []journalEntry
	nextSeq uint64
}

func journalDir(datadir string) string {
	return filepath.Join(datadir, "journal")
}

func inboxDir(datadir string) string {
	return filepath.Join(datadir, "inbox")
}

func NewOutboxJournal(datadir, peerHex string) (*OutboxJournal, error) {
	dir := journalDir(datadir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	j := &OutboxJournal{
		path:    filepath.Join(dir, peerHex+".jsonl"),
		seqPath: filepath.Join(dir, peerHex+".seq"),
	}
	if err := j.load(); err != nil {
		return nil, err
	}
	return j, nil
}

func (j *OutboxJournal) load() error {
	data, err := os.ReadFile(j.path)
	if err == nil {
		for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
			if line == "" {
				continue
			}
			var e journalEntry
			if err := json.Unmarshal([]byte(line), &e); err != nil {
				return fmt.Errorf("parse %s: %w", j.path, err)
			}
			j.entries = append(j.entries, e)
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	j.nextSeq = 1
	for _, e := range j.entries {
		if e.Seq >= j.nextSeq {
			j.nextSeq = e.Seq + 1
		}
	}
	if seqData, err := os.ReadFile(j.seqPath); err == nil {
		var stored uint64
		if _, err := fmt.Sscanf(strings.TrimSpace(string(seqData)), "%d", &stored); err == nil {
			if stored > j.nextSeq {
				j.nextSeq = stored
			}
		}
	}
	return nil
}

func (j *OutboxJournal) NextSeq() (uint64, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	seq := j.nextSeq
	j.nextSeq++
	if err := os.WriteFile(j.seqPath, []byte(fmt.Sprintf("%d\n", j.nextSeq)), 0o600); err != nil {
		j.nextSeq--
		return 0, fmt.Errorf("persist seq: %w", err)
	}
	return seq, nil
}

func (j *OutboxJournal) Queue(seq uint64, ts int64, payload []byte) error {
	entry := journalEntry{Seq: seq, Ts: ts, Payload: base64.StdEncoding.EncodeToString(payload)}
	j.mu.Lock()
	defer j.mu.Unlock()
	data, err := json.Marshal(entry)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(j.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write(append(data, '\n')); err != nil {
		return err
	}
	j.entries = append(j.entries, entry)
	return nil
}

func (j *OutboxJournal) Pending() []journalEntry {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := make([]journalEntry, len(j.entries))
	copy(out, j.entries)
	return out
}

func (j *OutboxJournal) Ack(seq uint64) (bool, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	found := -1
	for i, e := range j.entries {
		if e.Seq == seq {
			found = i
			break
		}
	}
	if found < 0 {
		return false, nil
	}
	j.entries = append(j.entries[:found], j.entries[found+1:]...)
	data := make([]byte, 0, 256*len(j.entries))
	for _, e := range j.entries {
		line, err := json.Marshal(e)
		if err != nil {
			return true, err
		}
		data = append(data, append(line, '\n')...)
	}
	tmp := j.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return true, err
	}
	if err := os.Rename(tmp, j.path); err != nil {
		return true, err
	}
	return true, nil
}

type InboxStore struct {
	mu          sync.Mutex
	path        string
	highSeqPath string
	capacity    int
	entries     []journalEntry
	dedup       map[uint64]struct{}
	seenBelow   map[uint64]struct{}
	seenCap     int
	retainedMin uint64
}

type highSeqFile struct {
	RetainedMin uint64   `json:"retained_min"`
	Seen        []uint64 `json:"seen"`
}

func NewInboxStore(datadir, peerHex string, capacity int) (*InboxStore, error) {
	dir := inboxDir(datadir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	in := &InboxStore{
		path:        filepath.Join(dir, peerHex+".jsonl"),
		highSeqPath: filepath.Join(dir, peerHex+".highseq"),
		capacity:    capacity,
		dedup:       make(map[uint64]struct{}),
		seenBelow:   make(map[uint64]struct{}),
		seenCap:     4 * capacity,
	}
	if err := in.load(); err != nil {
		return nil, err
	}
	return in, nil
}

func (in *InboxStore) load() error {
	data, err := os.ReadFile(in.path)
	if err == nil {
		for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
			if line == "" {
				continue
			}
			var e journalEntry
			if err := json.Unmarshal([]byte(line), &e); err != nil {
				return fmt.Errorf("parse %s: %w", in.path, err)
			}
			in.entries = append(in.entries, e)
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	sort.Slice(in.entries, func(i, k int) bool { return in.entries[i].Seq < in.entries[k].Seq })
	if scData, err := os.ReadFile(in.highSeqPath); err == nil {
		var sc highSeqFile
		if err := json.Unmarshal(scData, &sc); err == nil {
			for _, s := range sc.Seen {
				in.seenBelow[s] = struct{}{}
			}
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	in.trimLocked()
	for _, e := range in.entries {
		in.dedup[e.Seq] = struct{}{}
	}
	return nil
}

// trimLocked drops the oldest entries past capacity, remembering their seqs in
// the seen set so a later replay (which is no longer in `entries`) is rejected.
func (in *InboxStore) trimLocked() {
	for len(in.entries) > in.capacity {
		oldest := in.entries[0]
		in.entries = in.entries[1:]
		delete(in.dedup, oldest.Seq)
		in.addSeenLocked(oldest.Seq)
	}
	if len(in.entries) > 0 {
		in.retainedMin = in.entries[0].Seq
	}
}

// addSeenLocked records a seq that was accepted but now sits below the retained
// window, bounded to seenCap entries (replay protection is windowed, not absolute).
func (in *InboxStore) addSeenLocked(seq uint64) {
	if _, ok := in.seenBelow[seq]; ok {
		return
	}
	if len(in.seenBelow) >= in.seenCap {
		var oldest uint64
		first := true
		for s := range in.seenBelow {
			if first || s < oldest {
				oldest, first = s, false
			}
		}
		delete(in.seenBelow, oldest)
	}
	in.seenBelow[seq] = struct{}{}
}

func (in *InboxStore) persistLocked() error {
	data := make([]byte, 0, 256*len(in.entries))
	for _, e := range in.entries {
		line, err := json.Marshal(e)
		if err != nil {
			return err
		}
		data = append(data, append(line, '\n')...)
	}
	tmp := in.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, in.path); err != nil {
		return err
	}
	sc := highSeqFile{RetainedMin: in.retainedMin, Seen: in.sortedSeenLocked()}
	scData, err := json.Marshal(sc)
	if err != nil {
		return err
	}
	tmp2 := in.highSeqPath + ".tmp"
	if err := os.WriteFile(tmp2, scData, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp2, in.highSeqPath)
}

func (in *InboxStore) sortedSeenLocked() []uint64 {
	out := make([]uint64, 0, len(in.seenBelow))
	for s := range in.seenBelow {
		out = append(out, s)
	}
	sort.Slice(out, func(i, k int) bool { return out[i] < out[k] })
	return out
}

func (in *InboxStore) Add(seq uint64, ts int64, payload []byte) (bool, error) {
	in.mu.Lock()
	defer in.mu.Unlock()
	if _, ok := in.dedup[seq]; ok {
		return false, nil
	}
	if seq < in.retainedMin {
		if _, ok := in.seenBelow[seq]; ok {
			return false, nil
		}
		in.addSeenLocked(seq)
	}
	entry := journalEntry{Seq: seq, Ts: ts, Payload: base64.StdEncoding.EncodeToString(payload)}
	idx := sort.Search(len(in.entries), func(i int) bool { return in.entries[i].Seq > seq })
	in.entries = append(in.entries, journalEntry{})
	copy(in.entries[idx+1:], in.entries[idx:])
	in.entries[idx] = entry
	in.dedup[seq] = struct{}{}
	in.trimLocked()
	if err := in.persistLocked(); err != nil {
		return true, err
	}
	return true, nil
}

func (in *InboxStore) After(afterSeq uint64) []journalEntry {
	in.mu.Lock()
	defer in.mu.Unlock()
	idx := sort.Search(len(in.entries), func(i int) bool { return in.entries[i].Seq > afterSeq })
	out := make([]journalEntry, len(in.entries)-idx)
	copy(out, in.entries[idx:])
	return out
}

func (in *InboxStore) Oldest() uint64 {
	in.mu.Lock()
	defer in.mu.Unlock()
	if len(in.entries) == 0 {
		return 0
	}
	return in.entries[0].Seq
}

func (in *InboxStore) Latest() uint64 {
	in.mu.Lock()
	defer in.mu.Unlock()
	if len(in.entries) == 0 {
		return 0
	}
	return in.entries[len(in.entries)-1].Seq
}
