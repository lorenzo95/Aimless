package main

import (
	"bytes"
	"errors"
	"testing"
)

func TestEnvelopeRoundtrip(t *testing.T) {
	cases := []Envelope{
		{Version: 1, Type: TypeMsg, Seq: 1, Ts: 1725000000000, Payload: []byte("hello")},
		{Version: 1, Type: TypeAck, Seq: 42},
		{Version: 1, Type: TypeStatus, Seq: 7, Ts: 1, Payload: []byte("status-blob")},
		{Version: 1, Type: TypeProbe},
		{Version: 1, Type: TypeMsg, Seq: 9, Payload: bytes.Repeat([]byte{0xAB}, 65535)},
	}
	for i, env := range cases {
		data, err := env.Encode()
		if err != nil {
			t.Fatalf("case %d encode: %v", i, err)
		}
		decoded, err := DecodeEnvelope(data)
		if err != nil {
			t.Fatalf("case %d decode: %v", i, err)
		}
		if decoded.Version != env.Version || decoded.Type != env.Type || decoded.Seq != env.Seq || decoded.Ts != env.Ts || !bytes.Equal(decoded.Payload, env.Payload) {
			t.Fatalf("case %d mismatch: %+v vs %+v", i, decoded, env)
		}
	}
}

func TestEnvelopeErrors(t *testing.T) {
	if _, err := (&Envelope{Payload: make([]byte, 65536)}).Encode(); !errors.Is(err, ErrOversized) {
		t.Fatalf("want ErrOversized, got %v", err)
	}
	if _, err := DecodeEnvelope([]byte{1, 1}); !errors.Is(err, ErrTruncated) {
		t.Fatalf("want ErrTruncated, got %v", err)
	}
	trunc := make([]byte, 20)
	trunc[0] = 1
	trunc[1] = byte(TypeMsg)
	trunc[18] = 5
	trunc[19] = 0
	if _, err := DecodeEnvelope(trunc); !errors.Is(err, ErrTruncated) {
		t.Fatalf("want ErrTruncated for short payload, got %v", err)
	}
	bad := make([]byte, 25)
	bad[0] = 2
	if _, err := DecodeEnvelope(bad); !errors.Is(err, ErrBadVersion) {
		t.Fatalf("want ErrBadVersion, got %v", err)
	}
}
