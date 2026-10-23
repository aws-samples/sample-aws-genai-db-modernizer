package main

// Valkey 16-stream-on-cluster benchmark (apples-to-apples vs Kafka 16 partitions).
//   MODE=produce : write COUNT flat events across N streams on the broker CLUSTER (id % N)
//   MODE=consume : N consumers, EACH reads ONE stream per XREADGROUP (no CROSSSLOT),
//                  HSET to the 6-shard target cluster, XACK.
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
)

var written int64

func clusterClient(addr string, pool int) *redis.ClusterClient {
	return redis.NewClusterClient(&redis.ClusterOptions{
		Addrs: []string{addr}, PoolSize: pool,
		ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second,
		TLSConfig: &tls.Config{InsecureSkipVerify: true},
	})
}

func main() {
	mode := envOr("MODE", "consume")
	brokerAddr := envOr("BROKER_ADDR", "127.0.0.1:6379")
	count, _ := strconv.Atoi(envOr("COUNT", "1000000"))
	streams, _ := strconv.Atoi(envOr("STREAMS", "16"))
	prefix := envOr("STREAM_PREFIX", "data:part:")

	ctx := context.Background()
	broker := clusterClient(brokerAddr, streams+8)
	if err := broker.Ping(ctx).Err(); err != nil {
		fmt.Printf("broker ping failed: %v\n", err)
		os.Exit(1)
	}

	if mode == "produce" {
		produce(ctx, broker, count, streams, prefix)
		return
	}
	consume(ctx, broker, count, streams, prefix)
}

func produce(ctx context.Context, broker *redis.ClusterClient, count, streams int, prefix string) {
	for i := 0; i < streams; i++ {
		broker.Del(ctx, fmt.Sprintf("%s%d", prefix, i))
	}
	gor, _ := strconv.Atoi(envOr("GEN_WORKERS", "8"))
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
			pipe := broker.Pipeline()
			n := 0
			for id := s; id < e; id++ {
				val := fmt.Sprintf(`{"id":%d,"first_name":"C_%d","last_name":"L_%d","email":"c%d@t.com","created_at":"2026-06-24T20:05:39Z"}`, id, id, id, id)
				key := fmt.Sprintf(`{"id":%d}`, id)
				stream := fmt.Sprintf("%s%d", prefix, id%streams)
				pipe.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]interface{}{"key": key, "value": val}})
				n++
				if n%2000 == 0 {
					pipe.Exec(ctx)
					pipe = broker.Pipeline()
				}
			}
			pipe.Exec(ctx)
		}(s, e)
	}
	wg.Wait()
	total := 0
	for i := 0; i < streams; i++ {
		l, _ := broker.XLen(ctx, fmt.Sprintf("%s%d", prefix, i)).Result()
		total += int(l)
	}
	fmt.Printf("Produced %d across %d streams in %.1fs (total in broker=%d)\n", count, streams, time.Since(start).Seconds(), total)
}

func consume(ctx context.Context, broker *redis.ClusterClient, count, streams int, prefix string) {
	batch, _ := strconv.Atoi(envOr("BATCH_SIZE", "20000"))
	group := envOr("CONSUMER_GROUP", "g-multi")
	tgtAddr := envOr("TARGET_ADDR", "127.0.0.1:6379")
	streamTable := envOr("STREAM_TABLE", "customers")

	target := clusterClient(tgtAddr, streams+8)
	if err := target.Ping(ctx).Err(); err != nil {
		fmt.Printf("target ping failed: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Consuming %d streams (one reader each), batch %d -> 6-shard target\n", streams, batch)

	// Pre-create groups + read each stream length.
	lens := make([]int64, streams)
	for i := 0; i < streams; i++ {
		sk := fmt.Sprintf("%s%d", prefix, i)
		broker.XGroupCreateMkStream(ctx, sk, group, "0")
		broker.XGroupSetID(ctx, sk, group, "0")
		l, _ := broker.XLen(ctx, sk).Result()
		lens[i] = l
	}

	start := time.Now()
	var wg sync.WaitGroup
	for i := 0; i < streams; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			sk := fmt.Sprintf("%s%d", prefix, idx)
			consumer := fmt.Sprintf("c%d", idx)
			var got int64
			for got < lens[idx] {
				res, err := broker.XReadGroup(ctx, &redis.XReadGroupArgs{
					Group: group, Consumer: consumer,
					Streams: []string{sk, ">"}, Count: int64(batch), Block: 500 * time.Millisecond,
				}).Result()
				if err != nil || len(res) == 0 {
					continue
				}
				msgs := res[0].Messages
				if len(msgs) == 0 {
					continue
				}
				pipe := target.Pipeline()
				ackIDs := make([]string, 0, len(msgs))
				for _, m := range msgs {
					ackIDs = append(ackIDs, m.ID)
					val, _ := m.Values["value"].(string)
					var row map[string]interface{}
					if json.Unmarshal([]byte(val), &row) != nil || row == nil {
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
				broker.XAck(ctx, sk, group, ackIDs...)
				got += int64(len(msgs))
				atomic.AddInt64(&written, int64(len(msgs)))
			}
		}(i)
	}

	go func() {
		t := time.NewTicker(5 * time.Second)
		defer t.Stop()
		for {
			<-t.C
			w := atomic.LoadInt64(&written)
			el := time.Since(start).Seconds()
			fmt.Printf("  [%.0fs] written=%d rate=%.0f/sec\n", el, w, float64(w)/el)
		}
	}()

	wg.Wait()
	el := time.Since(start).Seconds()
	w := atomic.LoadInt64(&written)
	fmt.Printf("=== VALKEY %d-STREAM DONE: written=%d in %.2fs = %.0f/sec ===\n", streams, w, el, float64(w)/el)
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
