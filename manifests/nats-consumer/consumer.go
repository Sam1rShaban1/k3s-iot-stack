package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nats-io/nats.go"
)

const (
	StreamName   = "IOT_DATA"
	SubjectName  = "iot.data"
	ConsumerName = "metrics-consumer-go"
	VMUrl        = "http://victoriametrics-victoria-metrics-single-server.victoriametrics.svc.cluster.local:8428"
)

type SensorData struct {
	DeviceID       string  `json:"device_id"`
	TS             int64   `json:"ts"`
	Temperature    float64 `json:"temperature"`
	Humidity       float64 `json:"humidity"`
	Pressure       float64 `json:"pressure"`
	BenthosEntryTS int64   `json:"benthos_entry_ts"`
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lshortfile)
	log.SetOutput(os.Stdout)

	natsUrl := os.Getenv("NATS_URL")
	if natsUrl == "" {
		natsUrl = "nats://nats.nats.svc.cluster.local:4222"
	}

	vmUrl := os.Getenv("VICTORIA_METRICS_URL")
	if vmUrl == "" {
		vmUrl = VMUrl + "/api/v1/import/prometheus"
	}

	batchSize := 2000
	if bs := os.Getenv("BATCH_SIZE"); bs != "" {
		if v, err := fmt.Sscanf(bs, "%d", &batchSize); err == nil && v > 0 {
			log.Printf("Using batch size: %d", batchSize)
		}
	}

	log.Printf("Starting Go NATS consumer, connecting to: %s", natsUrl)
	log.Printf("VictoriaMetrics URL: %s", vmUrl)

	nc, err := nats.Connect(natsUrl,
		nats.Name("nats-consumer-go"),
		nats.MaxReconnects(60),
		nats.ReconnectWait(2*time.Second),
	)
	if err != nil {
		log.Fatalf("Failed to connect to NATS: %v", err)
	}
	log.Println("Connected to NATS!")
	defer nc.Close()

	js, err := nc.JetStream()
	if err != nil {
		log.Fatalf("Failed to get JetStream: %v", err)
	}

	_, err = js.AddStream(&nats.StreamConfig{
		Name:     StreamName,
		Subjects: []string{SubjectName},
	})
	if err != nil {
		log.Printf("Stream may already exist: %v", err)
	}

	cons, err := js.PullSubscribe(SubjectName, ConsumerName,
		nats.AckExplicit(),
	)
	if err != nil {
		log.Fatalf("Failed to subscribe: %v", err)
	}
	log.Println("Subscribed to NATS, starting fetch loop")

	var (
		msgCount   int64
		vmSuccess  int64
		vmFail     int64
	)

	batch := make([][]byte, 0, batchSize)
	var batchMu sync.Mutex

	httpClient := &http.Client{Timeout: 30 * time.Second}
	ctx := context.Background()

	sendBatch := func(b [][]byte) {
		if len(b) == 0 {
			return
		}

		lines := make([]string, 0, len(b)*7)
		entryTs := time.Now().UnixMilli()

		for _, data := range b {
			var sd SensorData
			if err := json.Unmarshal(data, &sd); err != nil {
				continue
			}
			lines = append(lines,
				fmt.Sprintf("iot_sensor_ts{device_id=\"%s\",msg_id=\"%d\"} %d", sd.DeviceID, sd.TS, sd.TS),
				fmt.Sprintf("iot_sensor_temp{device_id=\"%s\",msg_id=\"%d\"} %.2f", sd.DeviceID, sd.TS, sd.Temperature),
				fmt.Sprintf("iot_sensor_hum{device_id=\"%s\",msg_id=\"%d\"} %.2f", sd.DeviceID, sd.TS, sd.Humidity),
				fmt.Sprintf("iot_sensor_nats_exit_ts{device_id=\"%s\",msg_id=\"%d\"} %d", sd.DeviceID, sd.TS, entryTs),
			)
		}

		body := strings.NewReader(strings.Join(lines, "\n"))
		req, _ := http.NewRequestWithContext(ctx, "POST", vmUrl, body)
		req.Header.Set("Content-Type", "text/plain")
		resp, err := httpClient.Do(req)
		if err != nil {
			log.Printf("VM send error: %v", err)
			atomic.AddInt64(&vmFail, int64(len(b)))
			return
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			atomic.AddInt64(&vmSuccess, int64(len(b)))
		} else {
			log.Printf("VM HTTP error: status=%d", resp.StatusCode)
			atomic.AddInt64(&vmFail, int64(len(b)))
		}
	}

	go func() {
		t := time.NewTicker(50 * time.Millisecond)
		defer t.Stop()
		for range t.C {
			batchMu.Lock()
			if len(batch) >= batchSize {
				currentBatch := batch[:batchSize]
				batch = batch[batchSize:]
				go sendBatch(currentBatch)
			}
			batchMu.Unlock()
		}
	}()

	go func() {
		for {
			msgs, err := cons.Fetch(100, nats.MaxWait(5*time.Second))
			if err != nil {
				log.Printf("Fetch error: %v", err)
				if err == nats.ErrTimeout {
					continue
				}
				time.Sleep(time.Second)
				continue
			}

			log.Printf("Got %d messages", len(msgs))

			for _, msg := range msgs {
				atomic.AddInt64(&msgCount, 1)
				batchMu.Lock()
				batch = append(batch, msg.Data)
				if len(batch) >= batchSize {
					currentBatch := batch[:batchSize]
					batch = batch[batchSize:]
					go sendBatch(currentBatch)
				}
				batchMu.Unlock()
				msg.Ack()

				if atomic.LoadInt64(&msgCount)%1000 == 0 {
					log.Printf("Received: %d, VM ok: %d, failed: %d",
						atomic.LoadInt64(&msgCount),
						atomic.LoadInt64(&vmSuccess),
						atomic.LoadInt64(&vmFail))
				}
			}
		}
	}()

	go func() {
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			batchMu.Lock()
			if len(batch) > 0 {
				currentBatch := make([][]byte, len(batch))
				copy(currentBatch, batch)
				batch = batch[:0]
				go sendBatch(currentBatch)
			}
			batchMu.Unlock()
		}
	}()

	log.Println("Consumer ready, waiting for messages...")
	<-make(chan struct{})
}