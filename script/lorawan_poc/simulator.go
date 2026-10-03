package main

import (
	"context"
	"encoding/hex"
	"sync"
	"time"

	"github.com/brocaar/chirpstack-simulator/simulator"
	"github.com/brocaar/lorawan"
	"github.com/chirpstack/chirpstack/api/go/v4/gw"
	log "github.com/sirupsen/logrus"
)

// This example simulates an OTAA activation and the sending of a single uplink
// frame, after which the simulation terminates.
func main() {
	gatewayID := lorawan.EUI64{1, 1, 1, 1, 1, 1, 1, 1}
	devEUI := lorawan.EUI64{2, 1, 1, 1, 1, 1, 1, 1}
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
		simulator.WithUplinkCount(1),
		simulator.WithUplinkPayload(false, 1, []byte{0x01,0x01,0x10,0x98,0x53,0,0,0x01,0x02,0x10,0xA8,0x7A,0,0,0xAF,0x51}),
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

			return nil
		}),
	)
	if err != nil {
		panic(err)
	}

	wg.Wait()
}
