package main

// Generates N synthetic flat CDC events into a Valkey stream, matching the
// format the sink worker expects: a "key" field ({"id":N}) and a "value" field
// (the flat row JSON). Used to benchmark sink throughput at scale.
import (
	"context"
	"fmt"
	"os"
	"strconv"
	"sync"

	"github.com/redis/go-redis/v9"
)

func main() {
	addr := envOr("BROKER_ADDR", "localhost:6380")
	stream := envOr("STREAM", "data:ht.Inventory.customers")
	count, _ := strconv.Atoi(envOr("COUNT", "1000000"))
	gor, _ := strconv.Atoi(envOr("GEN_WORKERS", "8"))

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
			for id := start; id < end; id++ {
				val := fmt.Sprintf(`{"id":%d,"first_name":"C_%d","last_name":"L_%d","email":"c%d@t.com","created_at":"2026-06-24T20:05:39Z"}`, id, id, id, id)
				key := fmt.Sprintf(`{"id":%d}`, id)
				pipe.XAdd(ctx, &redis.XAddArgs{
					Stream: stream,
					Values: map[string]interface{}{"key": key, "value": val},
				})
				if (id-start+1)%2000 == 0 {
					pipe.Exec(ctx)
					pipe = c.Pipeline()
				}
			}
			pipe.Exec(ctx)
		}(start, end)
	}
	wg.Wait()
	n, _ := c.XLen(ctx, stream).Result()
	fmt.Printf("Generated. stream=%s XLEN=%d\n", stream, n)
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
