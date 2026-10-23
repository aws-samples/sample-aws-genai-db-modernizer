package main

// Splits a source Valkey stream into N partitions by id % N, writing each
// partition to a separate broker. Preserves the "key" and "value" fields.
import (
	"context"
	"fmt"
	"os"
	"strconv"

	json "github.com/goccy/go-json"
	"github.com/redis/go-redis/v9"
)

func main() {
	srcAddr := envOr("SRC_ADDR", "localhost:6380")
	srcStream := envOr("SRC_STREAM", "data:ht.Inventory.customers")
	// Comma-separated destination broker addresses, one per partition.
	dstAddrs := []string{
		envOr("DST0", "localhost:6380"),
		envOr("DST1", "localhost:6391"),
		envOr("DST2", "localhost:6392"),
	}
	dstStream := envOr("DST_STREAM", "part")
	n := len(dstAddrs)

	ctx := context.Background()
	src := redis.NewClient(&redis.Options{Addr: srcAddr})
	dsts := make([]*redis.Client, n)
	for i, a := range dstAddrs {
		dsts[i] = redis.NewClient(&redis.Options{Addr: a})
		if err := dsts[i].Ping(ctx).Err(); err != nil {
			fmt.Printf("dst %d (%s) ping failed: %v\n", i, a, err)
			os.Exit(1)
		}
		// Fresh partition streams.
		dsts[i].Del(ctx, fmt.Sprintf("%s:%d", dstStream, i))
	}

	lastID := "0"
	total := 0
	counts := make([]int, n)
	const page = 10000

	for {
		res, err := src.XRangeN(ctx, srcStream, "("+lastID, "+", page).Result()
		if err != nil {
			fmt.Printf("xrange err: %v\n", err)
			break
		}
		if len(res) == 0 {
			break
		}
		pipes := make([]redis.Pipeliner, n)
		for i := range dsts {
			pipes[i] = dsts[i].Pipeline()
		}
		for _, m := range res {
			lastID = m.ID
			val, _ := m.Values["value"].(string)
			keyStr, _ := m.Values["key"].(string)
			var row map[string]interface{}
			part := 0
			if json.Unmarshal([]byte(val), &row) == nil {
				if idv, ok := row["id"]; ok {
					if idi, ok2 := toInt(idv); ok2 {
						part = idi % n
					}
				}
			}
			pipes[part].XAdd(ctx, &redis.XAddArgs{
				Stream: fmt.Sprintf("%s:%d", dstStream, part),
				Values: map[string]interface{}{"key": keyStr, "value": val},
			})
			counts[part]++
			total++
		}
		for i := range pipes {
			pipes[i].Exec(ctx)
		}
		if total%200000 < page {
			fmt.Printf("  ...split %d\n", total)
		}
	}
	fmt.Printf("Split done. total=%d counts=%v\n", total, counts)
}

func toInt(v interface{}) (int, bool) {
	switch x := v.(type) {
	case float64:
		return int(x), true
	case int:
		return x, true
	case string:
		i, err := strconv.Atoi(x)
		return i, err == nil
	}
	return 0, false
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
