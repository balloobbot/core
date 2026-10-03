package main

import (
	"context"
	"encoding/hex"
	"sync"
	"sync/atomic"
	"time"

	"github.com/brocaar/chirpstack-simulator/simulator"
	"github.com/brocaar/lorawan"
	"github.com/chirpstack/chirpstack/api/go/v4/gw"
	log "github.com/sirupsen/logrus"
)

// Emulate two Dragino relays after OTAA; all commands arrive as encrypted downlinks.
func main() {
	gatewayID := lorawan.EUI64{1, 1, 1, 1, 1, 1, 1, 1}
	devEUI := lorawan.EUI64{2, 1, 1, 1, 1, 1, 1, 2}
	var relays atomic.Uint32
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
			data[8] = byte(relays.Load())
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

			if fPort == 2 && len(data) == 3 && data[0] == 0x03 {
				state := relays.Load()
				for index, mask := range []uint32{0x80, 0x40} {
					switch data[index+1] {
					case 0x00:
						state &^= mask
					case 0x01:
						state |= mask
					case 0x11:
						// Leave the other relay unchanged.
					default:
						return nil
					}
				}
				relays.Store(state)
			}
			return nil
		}),
	)
	if err != nil {
		panic(err)
	}

	wg.Wait()
}
