package main

// Kafka-vs-Valkey sink benchmark — FAIR mode: TLS + consumer-group commits.
//   MODE=produce : write COUNT flat CDC events to a 16-partition topic (keyed by id)
//   MODE=consume : consumer group (16 members), commit per batch, HSET to 6-shard target
import (
	"context"
	"crypto/tls"
	"fmt"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	json "github.com/goccy/go-json"
	"github.com/redis/go-redis/v9"
	"github.com/segmentio/kafka-go"
)

var consumed int64
var written int64

func tlsCfg() *tls.Config {
	if envOr("KAFKA_TLS", "false") == "true" {
		return &tls.Config{InsecureSkipVerify: true}
	}
	return nil
}

func main() {
	mode := envOr("MODE", "consume")
	broker := envOr("KAFKA", "localhost:9093")
	topic := envOr("TOPIC", "cdc-customers")
	count, _ := strconv.Atoi(envOr("COUNT", "1000000"))
	switch mode {
	case "produce":
		produce(broker, topic, count)
	case "consume":
		consume(broker, topic, count)
	default:
		fmt.Println("unknown MODE")
		os.Exit(1)
	}
}

func produce(broker, topic string, count int) {
	gor, _ := strconv.Atoi(envOr("PROD_WORKERS", "8"))
	w := &kafka.Writer{
		Addr:         kafka.TCP(broker),
		Topic:        topic,
		Balancer:     &kafka.Hash{},
		BatchSize:    5000,
		BatchBytes:   900000,
		RequiredAcks: kafka.RequireOne,
	}
	if t := tlsCfg(); t != nil {
		w.Transport = &kafka.Transport{TLS: t}
	}
	defer w.Close()
	fmt.Printf("Producing %d events to %s (TLS=%v)\n", count, topic, tlsCfg() != nil)
	perG := count / gor
	var wg sync.WaitGroup
	start := time.Now()
	for g := 0; g < gor; g++ {
		wg.Add(1)
		s := g*perG + 1
		e := s + perG
		if g == gor-1 {
			e = count + 1
		}
		go func(s, e int) {
			defer wg.Done()
			ctx := context.Background()
			msgs := make([]kafka.Message, 0, 5000)
			for id := s; id < e; id++ {
				val := fmt.Sprintf(`{"id":%d,"first_name":"C_%d","last_name":"L_%d","email":"c%d@t.com","created_at":"2026-06-24T20:05:39Z"}`, id, id, id, id)
				msgs = append(msgs, kafka.Message{Key: []byte(strconv.Itoa(id)), Value: []byte(val)})
				if len(msgs) >= 5000 {
					w.WriteMessages(ctx, msgs...)
					msgs = msgs[:0]
				}
			}
			if len(msgs) > 0 {
				w.WriteMessages(ctx, msgs...)
			}
		}(s, e)
	}
	wg.Wait()
	fmt.Printf("Produced %d in %.1fs\n", count, time.Since(start).Seconds())
}

func consume(broker, topic string, count int) {
	consumers, _ := strconv.Atoi(envOr("CONSUMERS", "16"))
	batch, _ := strconv.Atoi(envOr("BATCH_SIZE", "20000"))
	tgtHost := envOr("TARGET_HOST", "localhost")
	tgtPort := envOr("TARGET_PORT", "6379")
	streamTable := envOr("STREAM_TABLE", "customers")
	group := envOr("GROUP", "g-kafka-fair")

	target := redis.NewClusterClient(&redis.ClusterOptions{
		Addrs: []string{tgtHost + ":" + tgtPort}, PoolSize: consumers + 5,
		ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second,
		TLSConfig: &tls.Config{InsecureSkipVerify: true},
	})
	if err := target.Ping(context.Background()).Err(); err != nil {
		fmt.Printf("target ping failed: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Consuming %s: group=%s consumers=%d batch=%d TLS=%v + commit\n", topic, group, consumers, batch, tlsCfg() != nil)

	var dialer *kafka.Dialer
	if t := tlsCfg(); t != nil {
		dialer = &kafka.Dialer{Timeout: 10 * time.Second, DualStack: true, TLS: t}
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	start := time.Now()
	var wg sync.WaitGroup
	for c := 0; c < consumers; c++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rc := kafka.ReaderConfig{
				Brokers:  []string{broker},
				GroupID:  group,
				Topic:    topic,
				MinBytes: 1,
				MaxBytes: 16 << 20,
				MaxWait:  500 * time.Millisecond,
			}
			if dialer != nil {
				rc.Dialer = dialer
			}
			r := kafka.NewReader(rc)
			defer r.Close()
			buf := make([]kafka.Message, 0, batch)
			flush := func() {
				if len(buf) == 0 {
					return
				}
				pipe := target.Pipeline()
				for _, m := range buf {
					var row map[string]interface{}
					if json.Unmarshal(m.Value, &row) != nil || row == nil {
						continue
					}
					pk := fmt.Sprintf("%v", row["id"])
					fields := make(map[string]interface{}, len(row))
					for k, v := range row {
						if v == nil {
							fields[k] = ""
						} else {
							fields[k] = fmt.Sprintf("%v", v)
						}
					}
					pipe.HSet(ctx, streamTable+":"+pk, fields)
				}
				pipe.Exec(ctx)
				r.CommitMessages(ctx, buf...) // <-- ack/commit, like Valkey XACK
				atomic.AddInt64(&written, int64(len(buf)))
				buf = buf[:0]
			}
			for {
				if atomic.LoadInt64(&consumed) >= int64(count) {
					flush()
					return
				}
				fctx, fc := context.WithTimeout(ctx, 2*time.Second)
				m, err := r.FetchMessage(fctx)
				fc()
				if err != nil {
					flush()
					if atomic.LoadInt64(&consumed) >= int64(count) {
						return
					}
					continue
				}
				buf = append(buf, m)
				atomic.AddInt64(&consumed, 1)
				if len(buf) >= batch {
					flush()
				}
			}
		}()
	}

	go func() {
		t := time.NewTicker(5 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				w := atomic.LoadInt64(&written)
				el := time.Since(start).Seconds()
				fmt.Printf("  [%.0fs] written=%d rate=%.0f/sec\n", el, w, float64(w)/el)
			}
		}
	}()

	for atomic.LoadInt64(&consumed) < int64(count) {
		time.Sleep(200 * time.Millisecond)
	}
	cancel()
	wg.Wait()
	el := time.Since(start).Seconds()
	w := atomic.LoadInt64(&written)
	fmt.Printf("=== KAFKA(TLS+commit) DONE: written=%d in %.2fs = %.0f/sec ===\n", w, el, float64(w)/el)
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
