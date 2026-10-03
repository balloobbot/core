package main

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"sync"
	"sync/atomic"
	"time"

	"github.com/brocaar/chirpstack-simulator/simulator"
	"github.com/brocaar/lorawan"
	"github.com/chirpstack/chirpstack/api/go/v4/gw"
	log "github.com/sirupsen/logrus"
)

// Emulate LT-22222-L I/O after OTAA; commands arrive as encrypted downlinks.
func main() {
	gatewayID := lorawan.EUI64{1, 1, 1, 1, 1, 1, 1, 1}
	devEUI := lorawan.EUI64{2, 1, 1, 1, 1, 1, 1, 2}
	var outputs atomic.Uint32
	appKey := lorawan.AES128Key{3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1}

	var wg sync.WaitGroup
	ctx := context.Background()

	sgw, err := simulator.NewGateway(
		simulator.WithMQTTCredentials("127.0.0.1:11883", "", ""),
		simulator.WithGatewayID(gatewayID),
		simulator.WithEventTopicTemplate("eu868/gateway/{{ .GatewayID }}/event/{{ .Event }}"),
		simulator.WithCommandTopicTemplate("eu868/gateway/{{ .GatewayID }}/command/{{ .Command }}"),
	)
	if err != nil {
		panic(err)
	}

	_, err = simulator.NewDevice(ctx, &wg,
		simulator.WithDevEUI(devEUI),
		simulator.WithAppKey(appKey),
		simulator.WithRandomDevNonce(),
		simulator.WithUplinkInterval(time.Second),
		simulator.WithUplinkCount(0),
		simulator.WithUplinkPayload(false, 2, nil),
		simulator.WithUplinkPayloadFunc(func() []byte {
			data := make([]byte, 11)
			binary.BigEndian.PutUint16(data[0:2], 1195)
			binary.BigEndian.PutUint16(data[2:4], 1196)
			binary.BigEndian.PutUint16(data[4:6], 4880)
			binary.BigEndian.PutUint16(data[6:8], 4864)
			data[8] = byte(outputs.Load()) | 0x08
			data[10] = 0x41
			return data
		}),
		simulator.WithUplinkTXInfo(gw.UplinkTxInfo{
			Frequency: 868100000,
			Modulation: &gw.Modulation{
				Parameters: &gw.Modulation_Lora{
					Lora: &gw.LoraModulationInfo{
						Bandwidth:       125000,
						SpreadingFactor: 7,
						CodeRate:        gw.CodeRate_CR_4_5,
					},
				},
			},
		}),
		simulator.WithGateways([]*simulator.Gateway{sgw}),
		simulator.WithDownlinkHandlerFunc(func(conf, ack bool, fCntDown uint32, fPort uint8, data []byte) error {
			log.WithFields(log.Fields{
				"ack":       ack,
				"fcnt_down": fCntDown,
				"f_port":    fPort,
				"data":      hex.EncodeToString(data),
			}).Info("WithDownlinkHandlerFunc triggered")

			if fPort != 2 || len(data) < 3 {
				return nil
			}
			var masks []uint32
			switch {
			case len(data) == 3 && data[0] == 0x03:
				masks = []uint32{0x80, 0x40}
			case len(data) == 4 && data[0] == 0x02:
				masks = []uint32{0x01, 0x02}
			default:
				return nil
			}
			{
				state := outputs.Load()
				for index, mask := range masks {
					switch data[index+1] {
					case 0x00:
						state &^= mask
					case 0x01:
						state |= mask
					case 0x11:
						// Leave the other output unchanged.
					default:
						return nil
					}
				}
				outputs.Store(state)
			}
			return nil
		}),
	)
	if err != nil {
		panic(err)
	}

	wg.Wait()
}
