package main

// Per-event end-to-end latency: producer -> broker -> consumer, on the same host
// (same clock). Steady, non-saturating rate so we measure latency, not queuing.
//   ENGINE=valkey : local standalone Valkey stream (localhost:6380, no TLS)
//   ENGINE=kafka  : local Kafka topic (localhost:9092, plaintext, per-event, no batch)
import (
	"context"
	"fmt"
	"os"
	"sort"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/segmentio/kafka-go"
)

func main() {
	engine := envOr("ENGINE", "valkey")
	rate, _ := strconv.Atoi(envOr("RATE", "5000"))
	dur, _ := strconv.Atoi(envOr("DURATION", "20"))
	warmup := 2 * time.Second

	lat := make([]int64, 0, rate*dur) // microseconds
	done := make(chan struct{})
	start := time.Now()

	switch engine {
	case "valkey":
		runValkey(rate, dur, warmup, start, &lat, done)
	case "kafka":
		runKafka(rate, dur, warmup, start, &lat, done)
	default:
		fmt.Println("unknown ENGINE")
		os.Exit(1)
	}

	report(engine, rate, lat)
}

func runValkey(rate, dur int, warmup time.Duration, start time.Time, lat *[]int64, done chan struct{}) {
	ctx := context.Background()
	addr := envOr("VALKEY_ADDR", "localhost:6380")
	stream := "lat:stream"
	group := "lg"
	c := redis.NewClient(&redis.Options{Addr: addr})
	if err := c.Ping(ctx).Err(); err != nil {
		fmt.Printf("valkey ping failed: %v\n", err)
		os.Exit(1)
	}
	c.Del(ctx, stream)
	c.XGroupCreateMkStream(ctx, stream, group, "$")

	// consumer
	go func() {
		for {
			res, err := c.XReadGroup(ctx, &redis.XReadGroupArgs{
				Group: group, Consumer: "c1", Streams: []string{stream, ">"},
				Count: 200, Block: 100 * time.Millisecond,
			}).Result()
			if err != nil || len(res) == 0 {
				select {
				case <-done:
					return
				default:
					continue
				}
			}
			now := time.Now().UnixNano()
			ids := make([]string, 0, len(res[0].Messages))
			for _, m := range res[0].Messages {
				ids = append(ids, m.ID)
				if tsStr, ok := m.Values["ts"].(string); ok {
					ts, _ := strconv.ParseInt(tsStr, 10, 64)
					if time.Duration(now-start.UnixNano()) > warmup {
						*lat = append(*lat, (now-ts)/1000) // us
					}
				}
			}
			c.XAck(ctx, stream, group, ids...)
			select {
			case <-done:
				return
			default:
			}
		}
	}()

	// producer at steady rate
	produceLoop(rate, dur, done, func() {
		c.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]interface{}{"ts": strconv.FormatInt(time.Now().UnixNano(), 10)}})
	})
}

func runKafka(rate, dur int, warmup time.Duration, start time.Time, lat *[]int64, done chan struct{}) {
	broker := envOr("KAFKA", "localhost:9092")
	topic := "lat-topic"
	w := &kafka.Writer{
		Addr: kafka.TCP(broker), Topic: topic,
		BatchSize: 1, BatchTimeout: 1 * time.Millisecond, // per-event, no batching
		RequiredAcks: kafka.RequireOne,
	}
	defer w.Close()

	r := kafka.NewReader(kafka.ReaderConfig{
		Brokers: []string{broker}, Topic: topic, Partition: 0,
		MinBytes: 1, MaxBytes: 10 << 20, MaxWait: 5 * time.Millisecond,
	})
	r.SetOffset(kafka.LastOffset) // only new messages
	defer r.Close()

	go func() {
		ctx := context.Background()
		for {
			m, err := r.ReadMessage(ctx)
			if err != nil {
				select {
				case <-done:
					return
				default:
					continue
				}
			}
			now := time.Now().UnixNano()
			ts, _ := strconv.ParseInt(string(m.Value), 10, 64)
			if time.Duration(now-start.UnixNano()) > warmup {
				*lat = append(*lat, (now-ts)/1000)
			}
			select {
			case <-done:
				return
			default:
			}
		}
	}()

	// give reader a moment to position at end
	time.Sleep(500 * time.Millisecond)
	produceLoop(rate, dur, done, func() {
		w.WriteMessages(context.Background(), kafka.Message{Value: []byte(strconv.FormatInt(time.Now().UnixNano(), 10))})
	})
}

func produceLoop(rate, dur int, done chan struct{}, send func()) {
	interval := time.Second / time.Duration(rate)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	deadline := time.Now().Add(time.Duration(dur) * time.Second)
	for range ticker.C {
		if time.Now().After(deadline) {
			break
		}
		send()
	}
	time.Sleep(1 * time.Second) // drain in-flight
	close(done)
	time.Sleep(300 * time.Millisecond)
}

func report(engine string, rate int, lat []int64) {
	if len(lat) == 0 {
		fmt.Println("no samples")
		return
	}
	sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
	p := func(q float64) int64 { return lat[int(float64(len(lat))*q)] }
	var sum int64
	for _, v := range lat {
		sum += v
	}
	fmt.Printf("=== %s latency (us) | samples=%d rate~%d/s ===\n", engine, len(lat), rate)
	fmt.Printf("  mean=%d  p50=%d  p90=%d  p99=%d  p999=%d  min=%d  max=%d\n",
		sum/int64(len(lat)), p(0.50), p(0.90), p(0.99), p(0.999), lat[0], lat[len(lat)-1])
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
