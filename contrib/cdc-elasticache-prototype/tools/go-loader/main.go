package main

// CDC-cache Go bulk loader — fast parallel initial load into a (sharded) Valkey target.
//
// Addresses the throughput levers over the Python loader:
//   • writes to a CLUSTER target (go-redis auto-routes each key to its shard →
//     writes fan out across all primaries)
//   • many goroutine workers over PK ranges (WORKERS)
//   • large pipelines to cut round-trips (BATCH)
//   • Go per-row cost is far below Python's
//
// Snapshot only (initial load). CDC handoff is handled by the orchestrator.
//
// Env:
//   SOURCE_DB_HOST/PORT/USER/PASSWORD/NAME, TABLE, PK, KEY_PREFIX
//   TARGET_ADDR (clustercfg host:port), TARGET_SSL (true/false), TARGET_CLUSTER (true/false)
//   WORKERS, BATCH
import (
	"context"
	"crypto/tls"
	"database/sql"
	"fmt"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	_ "github.com/go-sql-driver/mysql"
	"github.com/redis/go-redis/v9"
)

var written int64

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func main() {
	host := env("SOURCE_DB_HOST", "localhost")
	port := env("SOURCE_DB_PORT", "3306")
	user := env("SOURCE_DB_USER", "")
	pass := env("SOURCE_DB_PASSWORD", "")
	dbname := env("SOURCE_DB_NAME", "Inventory")
	table := env("TABLE", "orders")
	pk := env("PK", "id")
	prefix := env("KEY_PREFIX", table+":")
	targetAddr := env("TARGET_ADDR", "localhost:6379")
	targetSSL := env("TARGET_SSL", "true") == "true"
	targetCluster := env("TARGET_CLUSTER", "true") == "true"
	workers, _ := strconv.Atoi(env("WORKERS", "16"))
	batch, _ := strconv.Atoi(env("BATCH", "2000"))

	ctx := context.Background()
	dsn := fmt.Sprintf("%s:%s@tcp(%s:%s)/%s", user, pass, host, port, dbname)
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		fmt.Println("source open:", err)
		os.Exit(1)
	}
	db.SetMaxOpenConns(workers + 2)
	defer db.Close()

	var lo, hi sql.NullInt64
	if err := db.QueryRowContext(ctx,
		fmt.Sprintf("SELECT MIN(`%s`), MAX(`%s`) FROM `%s`", pk, pk, table)).Scan(&lo, &hi); err != nil {
		fmt.Println("bounds:", err)
		os.Exit(1)
	}
	if !lo.Valid {
		fmt.Println("table empty")
		return
	}
	// Optional PK bounds override (for benchmarking subsets, e.g. first 1M rows).
	if v := os.Getenv("PK_LO"); v != "" {
		if n, e := strconv.ParseInt(v, 10, 64); e == nil {
			lo.Int64 = n
		}
	}
	if v := os.Getenv("PK_HI"); v != "" {
		if n, e := strconv.ParseInt(v, 10, 64); e == nil {
			hi.Int64 = n
		}
	}
	fmt.Printf("Go loader | table=%s pk range [%d..%d] workers=%d batch=%d target=%s cluster=%v\n",
		table, lo.Int64, hi.Int64, workers, batch, targetAddr, targetCluster)

	var tlsConf *tls.Config
	if targetSSL {
		tlsConf = &tls.Config{InsecureSkipVerify: true}
	}
	var target redis.UniversalClient
	if targetCluster {
		target = redis.NewClusterClient(&redis.ClusterOptions{
			Addrs: []string{targetAddr}, PoolSize: workers + 4,
			ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second, TLSConfig: tlsConf,
		})
	} else {
		target = redis.NewClient(&redis.Options{
			Addr: targetAddr, PoolSize: workers + 4,
			ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second, TLSConfig: tlsConf,
		})
	}
	if err := target.Ping(ctx).Err(); err != nil {
		fmt.Println("target ping:", err)
		os.Exit(1)
	}

	// split [lo..hi] into contiguous ranges
	span := hi.Int64 - lo.Int64 + 1
	size := span / int64(workers)
	if size < 1 {
		size = 1
	}
	type rng struct{ lo, hi int64 }
	var ranges []rng
	for s := lo.Int64; s <= hi.Int64; s += size {
		e := s + size - 1
		if e > hi.Int64 {
			e = hi.Int64
		}
		ranges = append(ranges, rng{s, e})
	}

	start := time.Now()
	go func() {
		t := time.NewTicker(2 * time.Second)
		defer t.Stop()
		for range t.C {
			w := atomic.LoadInt64(&written)
			el := time.Since(start).Seconds()
			fmt.Printf("  [%.0fs] written=%d rate=%.0f/sec\n", el, w, float64(w)/el)
		}
	}()

	var wg sync.WaitGroup
	for _, r := range ranges {
		wg.Add(1)
		go func(r rng) {
			defer wg.Done()
			loadRange(ctx, db, target, table, pk, prefix, r.lo, r.hi, batch)
		}(r)
	}
	wg.Wait()

	el := time.Since(start).Seconds()
	w := atomic.LoadInt64(&written)
	fmt.Printf("=== DONE: loaded %d rows in %.2fs = %.0f rows/sec ===\n", w, el, float64(w)/el)
}

func loadRange(ctx context.Context, db *sql.DB, target redis.UniversalClient,
	table, pk, prefix string, lo, hi int64, batch int) {
	q := fmt.Sprintf("SELECT * FROM `%s` WHERE `%s` >= ? AND `%s` <= ?", table, pk, pk)
	rows, err := db.QueryContext(ctx, q, lo, hi)
	if err != nil {
		fmt.Println("query:", err)
		return
	}
	defer rows.Close()
	cols, _ := rows.Columns()
	raw := make([]sql.RawBytes, len(cols))
	scan := make([]interface{}, len(cols))
	for i := range scan {
		scan[i] = &raw[i]
	}
	pkIdx := -1
	for i, c := range cols {
		if c == pk {
			pkIdx = i
		}
	}
	pipe := target.Pipeline()
	n := 0
	for rows.Next() {
		if err := rows.Scan(scan...); err != nil {
			continue
		}
		fields := make(map[string]interface{}, len(cols))
		var pkVal string
		for i, c := range cols {
			if raw[i] == nil {
				fields[c] = ""
			} else {
				s := string(raw[i]) // copy out of RawBytes
				fields[c] = s
				if i == pkIdx {
					pkVal = s
				}
			}
		}
		pipe.HSet(ctx, prefix+pkVal, fields)
		n++
		if n >= batch {
			pipe.Exec(ctx)
			atomic.AddInt64(&written, int64(n))
			n = 0
			pipe = target.Pipeline()
		}
	}
	if n > 0 {
		pipe.Exec(ctx)
		atomic.AddInt64(&written, int64(n))
	}
}
