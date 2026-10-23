package main

// Generates Debezium-ENVELOPE CDC events into a Valkey stream for the
// denormalization benchmark (the denorm sink path decodes the envelope, not flat).
//   MODE=orders   : N order create events, customer_id = (id % FKMOD) + 1
//   MODE=custupd  : N customer UPDATE events (name/tier change) for ids 1..N
import (
	"context"
	"fmt"
	"os"
	"strconv"
	"sync"

	"github.com/redis/go-redis/v9"
)

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func main() {
	addr := env("BROKER_ADDR", "localhost:6380")
	stream := env("STREAM", "data:bench.orders")
	count, _ := strconv.Atoi(env("COUNT", "1000000"))
	gor, _ := strconv.Atoi(env("GEN_WORKERS", "8"))
	mode := env("MODE", "orders")
	fkmod, _ := strconv.Atoi(env("FKMOD", "100000"))

	ctx := context.Background()
	c := redis.NewClient(&redis.Options{Addr: addr, PoolSize: gor + 2})
	if err := c.Ping(ctx).Err(); err != nil {
		fmt.Printf("ping failed: %v\n", err)
		os.Exit(1)
	}
	c.Del(ctx, stream)

	perG := count / gor
	var wg sync.WaitGroup
	for g := 0; g < gor; g++ {
		wg.Add(1)
		start := g*perG + 1
		end := start + perG
		if g == gor-1 {
			end = count + 1
		}
		go func(start, end int) {
			defer wg.Done()
			pipe := c.Pipeline()
			n := 0
			for id := start; id < end; id++ {
				var val string
				key := fmt.Sprintf(`{"id":%d}`, id)
				if mode == "custupd" {
					before := fmt.Sprintf(`{"id":%d,"name":"N_%d","tier":"gold"}`, id, id)
					after := fmt.Sprintf(`{"id":%d,"name":"NN_%d","tier":"platinum"}`, id, id)
					val = fmt.Sprintf(`{"op":"u","source":{"table":"customers"},"before":%s,"after":%s}`, before, after)
				} else {
					cid := (id % fkmod) + 1
					after := fmt.Sprintf(`{"id":%d,"category":"food","amount":"5","customer_id":%d}`, id, cid)
					val = fmt.Sprintf(`{"op":"c","source":{"table":"orders"},"after":%s}`, after)
				}
				pipe.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]interface{}{"key": key, "value": val}})
				n++
				if n%2000 == 0 {
					pipe.Exec(ctx)
					pipe = c.Pipeline()
				}
			}
			pipe.Exec(ctx)
		}(start, end)
	}
	wg.Wait()
	l, _ := c.XLen(ctx, stream).Result()
	fmt.Printf("Generated mode=%s stream=%s XLEN=%d\n", mode, stream, l)
}
