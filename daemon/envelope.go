package main

import (
	"encoding/binary"
	"errors"
)

const envelopeVersion = 1

const envelopeHeaderSize = 20

const maxPayloadSize = 65535

type EnvelopeType uint8

const (
	TypeMsg    EnvelopeType = 1
	TypeAck    EnvelopeType = 2
	TypeStatus EnvelopeType = 3
	TypeProbe  EnvelopeType = 4
)

var ErrBadVersion = errors.New("bad envelope version")
var ErrTruncated = errors.New("truncated envelope")
var ErrOversized = errors.New("payload too large")

type Envelope struct {
	Version uint8
	Type    EnvelopeType
	Seq     uint64
	Ts      int64
	Payload []byte
}

func (e *Envelope) Encode() ([]byte, error) {
	if len(e.Payload) > maxPayloadSize {
		return nil, ErrOversized
	}
	buf := make([]byte, envelopeHeaderSize+len(e.Payload))
	buf[0] = e.Version
	buf[1] = byte(e.Type)
	binary.LittleEndian.PutUint64(buf[2:10], e.Seq)
	binary.LittleEndian.PutUint64(buf[10:18], uint64(e.Ts))
	binary.LittleEndian.PutUint16(buf[18:20], uint16(len(e.Payload)))
	copy(buf[20:], e.Payload)
	return buf, nil
}

func DecodeEnvelope(data []byte) (*Envelope, error) {
	if len(data) < envelopeHeaderSize {
		return nil, ErrTruncated
	}
	if data[0] != envelopeVersion {
		return nil, ErrBadVersion
	}
	payloadLen := int(binary.LittleEndian.Uint16(data[18:20]))
	if len(data) < envelopeHeaderSize+payloadLen {
		return nil, ErrTruncated
	}
	payload := make([]byte, payloadLen)
	copy(payload, data[envelopeHeaderSize:])
	return &Envelope{
		Version: data[0],
		Type:    EnvelopeType(data[1]),
		Seq:     binary.LittleEndian.Uint64(data[2:10]),
		Ts:      int64(binary.LittleEndian.Uint64(data[10:18])),
		Payload: payload,
	}, nil
}

func encodeAck(seq uint64) ([]byte, error) {
	return (&Envelope{Version: envelopeVersion, Type: TypeAck, Seq: seq}).Encode()
}
