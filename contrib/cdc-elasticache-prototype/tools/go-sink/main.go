package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	json "github.com/goccy/go-json"
	"github.com/redis/go-redis/v9"
)

// decodeJSON unmarshals with UseNumber so JSON numbers become json.Number
// (exact original text) instead of float64. This prevents large integer PKs
// and numeric fields from being rendered in scientific notation
// (e.g. 5177239 -> "5.177239e+06") when later formatted with %v.
func decodeJSON(b []byte, v interface{}) error {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	return dec.Decode(v)
}

var (
	writes      int64
	totalRead   int64 // nanoseconds
	totalParse  int64
	totalWrite  int64
	totalAck    int64
	cycles      int64
	batchSizes  int64
	deletes     int64

	transformSpec *TransformSpec
)

// loadSpecFromEnv loads the transform spec from TRANSFORMS_JSON (inline) or
// TRANSFORMS_FILE (path). Absent -> nil (passthrough).
func loadSpecFromEnv() *TransformSpec {
	if inline := os.Getenv("TRANSFORMS_JSON"); inline != "" {
		s, err := LoadTransformSpec([]byte(inline))
		if err != nil {
			fmt.Printf("  transform spec (env) parse error: %v — passthrough\n", err)
			return nil
		}
		return s
	}
	if path := os.Getenv("TRANSFORMS_FILE"); path != "" {
		b, err := os.ReadFile(path)
		if err != nil {
			fmt.Printf("  transform spec file %s not read: %v — passthrough\n", path, err)
			return nil
		}
		s, err := LoadTransformSpec(b)
		if err != nil {
			fmt.Printf("  transform spec file parse error: %v — passthrough\n", err)
			return nil
		}
		return s
	}
	return nil
}

type CDCEvent struct {
	After  map[string]interface{} `json:"after"`
	Before map[string]interface{} `json:"before"`
	Source struct {
		Table string `json:"table"`
	} `json:"source"`
	Op string `json:"op"`
}

func main() {
	brokerAddr := envOr("BROKER_HOST", "localhost") + ":" + envOr("BROKER_PORT", "6380")
	targetAddr := envOr("TARGET_HOST", "localhost") + ":" + envOr("TARGET_PORT", "6379")
	targetSSL := envOr("TARGET_SSL", "true") == "true"
	targetCluster := envOr("TARGET_CLUSTER", "false") == "true"
	batchSize, _ := strconv.Atoi(envOr("BATCH_SIZE", "5000"))
	workers, _ := strconv.Atoi(envOr("WORKERS", "16"))
	group := envOr("CONSUMER_GROUP", "go-timed")
	streamKey := envOr("STREAM_KEY", "data:ht.Inventory.customers")
	// STREAM_KEYS (comma-separated) enables multi-table consumption, needed for
	// denormalization (parent + child streams in one sink). Falls back to STREAM_KEY.
	streamKeys := []string{streamKey}
	if v := os.Getenv("STREAM_KEYS"); v != "" {
		streamKeys = streamKeys[:0]
		for _, s := range strings.Split(v, ",") {
			if s = strings.TrimSpace(s); s != "" {
				streamKeys = append(streamKeys, s)
			}
		}
	}
	eventFormat := envOr("EVENT_FORMAT", "envelope") // envelope | flat
	streamTable := envOr("STREAM_TABLE", "customers")

	fmt.Printf("Go Sink Worker — TIMED (per-step measurement)\n")
	fmt.Printf("  Broker: %s, Target: %s (TLS=%v)\n", brokerAddr, targetAddr, targetSSL)
	fmt.Printf("  Workers: %d, Batch: %d\n", workers, batchSize)

	// Health/metrics endpoint for ECS container health checks and monitoring.
	startHealthServer(envOr("HEALTH_PORT", "8081"))

	// Load the shared transform spec (passthrough if none configured).
	transformSpec = loadSpecFromEnv()
	if transformSpec != nil {
		fmt.Printf("  Transforms: %d table rule(s) loaded\n", len(transformSpec.Tables))
	} else {
		fmt.Println("  Transforms: none (passthrough)")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	broker := redis.NewClient(&redis.Options{
		Addr: brokerAddr, PoolSize: workers + 5,
		ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second,
	})
	broker.Ping(ctx)
	fmt.Println("  Broker connected")

	var tlsConf *tls.Config
	if targetSSL {
		tlsConf = &tls.Config{InsecureSkipVerify: true}
	}
	var target redis.UniversalClient
	if targetCluster {
		target = redis.NewClusterClient(&redis.ClusterOptions{
			Addrs: []string{targetAddr}, PoolSize: workers + 5,
			ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second,
			TLSConfig: tlsConf,
		})
	} else {
		target = redis.NewClient(&redis.Options{
			Addr: targetAddr, PoolSize: workers + 5,
			ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second,
			TLSConfig: tlsConf,
		})
	}
	target.Ping(ctx)
	fmt.Printf("  Target connected (cluster=%v)\n", targetCluster)

	// CDC freshness heartbeat: while the sink is alive and consuming, write a
	// liveness timestamp to the target so a downstream reader (e.g. the query
	// proxy) can detect a stalled/stopped pipeline and stop serving stale cache.
	if hbPrefix := os.Getenv("HEARTBEAT_PREFIX"); hbPrefix != "" {
		// Per-table heartbeat: <prefix>:<table> so independent CDC lanes are
		// tracked independently by downstream readers (e.g. the query proxy).
		hbKey := hbPrefix + ":" + streamTable
		go func() {
			t := time.NewTicker(2 * time.Second)
			defer t.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-t.C:
					target.Set(ctx, hbKey, strconv.FormatInt(time.Now().UnixMilli(), 10), 0)
				}
			}
		}()
		fmt.Printf("  CDC heartbeat -> %s (every 2s)\n", hbKey)
	}

	for _, sk := range streamKeys {
		broker.XGroupCreateMkStream(ctx, sk, group, "0").Err()
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	start := time.Now()

	// Consumer names must be unique across tasks so multiple sink tasks can
	// share one stream's load in the same group (horizontal scaling). Default
	// the prefix to the hostname, which ECS makes unique per task.
	consumerPrefix := os.Getenv("CONSUMER_PREFIX")
	if consumerPrefix == "" {
		if hn, err := os.Hostname(); err == nil && hn != "" {
			consumerPrefix = hn
		} else {
			consumerPrefix = "w"
		}
	}
	hasDenorm := specHasDenorm(transformSpec)
	if hasDenorm {
		fmt.Printf("  Denormalization: ON — multi-stream=%v\n", streamKeys)
	}
	for i := 0; i < workers; i++ {
		name := fmt.Sprintf("%s-w%d", consumerPrefix, i)
		if hasDenorm {
			go workerDenorm(ctx, broker, target, streamKeys, group, name, int64(batchSize))
		} else {
			go worker(ctx, broker, target, streamKey, group, name, int64(batchSize), eventFormat, streamTable)
		}
	}

	// Reliability: retry failed writes and dead-letter the ones that exhaust
	// MAX_DELIVERIES, so a change that can't be applied is never silently
	// dropped. (Denormalization path is a separate follow-up.)
	if !hasDenorm {
		go reclaimLoop(ctx, broker, target, []string{streamKey}, group, consumerPrefix+"-reclaim", eventFormat, streamTable)
		fmt.Printf("  Reliability: retry+DLQ ON (max_deliveries=%d, retry_idle=%v, dlq=%q)\n", maxDeliveries, retryIdle, dlqPrefix+streamKey)
	}

	// Stats every 5s
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				w := atomic.LoadInt64(&writes)
				c := atomic.LoadInt64(&cycles)
				elapsed := time.Since(start).Seconds()
				if c > 0 {
					avgRead := time.Duration(atomic.LoadInt64(&totalRead) / c)
					avgParse := time.Duration(atomic.LoadInt64(&totalParse) / c)
					avgWrite := time.Duration(atomic.LoadInt64(&totalWrite) / c)
					avgAck := time.Duration(atomic.LoadInt64(&totalAck) / c)
					avgBatch := atomic.LoadInt64(&batchSizes) / c
					d := atomic.LoadInt64(&deletes)
					fmt.Printf("  [%.0fs] writes=%d deletes=%d rate=%.0f/sec | per-cycle: read=%v parse=%v write=%v ack=%v batch=%d\n",
						elapsed, w, d, float64(w)/elapsed, avgRead, avgParse, avgWrite, avgAck, avgBatch)
				}
			}
		}
	}()

	<-sigCh
	cancel()
	elapsed := time.Since(start).Seconds()
	w := atomic.LoadInt64(&writes)
	c := atomic.LoadInt64(&cycles)
	fmt.Printf("\n=== FINAL ===\n")
	fmt.Printf("  Writes: %d in %.1fs = %.0f/sec\n", w, elapsed, float64(w)/elapsed)
	if c > 0 {
		fmt.Printf("  Cycles: %d\n", c)
		fmt.Printf("  Avg batch size: %d\n", atomic.LoadInt64(&batchSizes)/c)
		fmt.Printf("  Avg Read:  %v\n", time.Duration(atomic.LoadInt64(&totalRead)/c))
		fmt.Printf("  Avg Parse: %v\n", time.Duration(atomic.LoadInt64(&totalParse)/c))
		fmt.Printf("  Avg Write: %v\n", time.Duration(atomic.LoadInt64(&totalWrite)/c))
		fmt.Printf("  Avg Ack:   %v\n", time.Duration(atomic.LoadInt64(&totalAck)/c))
		total := atomic.LoadInt64(&totalRead) + atomic.LoadInt64(&totalParse) + atomic.LoadInt64(&totalWrite) + atomic.LoadInt64(&totalAck)
		fmt.Printf("  %% Read:  %.1f%%\n", 100*float64(atomic.LoadInt64(&totalRead))/float64(total))
		fmt.Printf("  %% Parse: %.1f%%\n", 100*float64(atomic.LoadInt64(&totalParse))/float64(total))
		fmt.Printf("  %% Write: %.1f%%\n", 100*float64(atomic.LoadInt64(&totalWrite))/float64(total))
		fmt.Printf("  %% Ack:   %.1f%%\n", 100*float64(atomic.LoadInt64(&totalAck))/float64(total))
	}
}

func worker(ctx context.Context, broker, target redis.UniversalClient, stream, group, name string, batch int64, eventFormat, streamTable string) {
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		// STEP 1: READ from broker
		t0 := time.Now()
		results, err := broker.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group: group, Consumer: name,
			Streams: []string{stream, ">"}, Count: batch, Block: 500 * time.Millisecond,
		}).Result()
		t1 := time.Now()

		if err != nil || len(results) == 0 {
			continue
		}

		readDur := t1.Sub(t0)
		msgs := results[0].Messages
		batchLen := int64(len(msgs))

		// STEP 2: PLAN each message (parse + transform). Unparseable /
		// structurally invalid messages ("poison") are collected for the DLQ
		// instead of being silently dropped.
		t2 := time.Now()
		type wcmd struct {
			id, key string
			fields  map[string]interface{}
		}
		type dcmd struct{ id, key string }
		wcmds := make([]wcmd, 0, batchLen)
		dcmds := make([]dcmd, 0)
		var poison []redis.XMessage
		for _, msg := range msgs {
			key, fields, isDel, isPoison := planMessage(msg, eventFormat, streamTable)
			switch {
			case isPoison:
				poison = append(poison, msg)
			case isDel:
				dcmds = append(dcmds, dcmd{msg.ID, key})
			default:
				wcmds = append(wcmds, wcmd{msg.ID, key, fields})
			}
		}
		t3 := time.Now()
		parseDur := t3.Sub(t2)

		// STEP 3: WRITE to target (pipeline). Capture per-command results so we
		// can ACK only the entries whose write actually succeeded.
		t4 := time.Now()
		pipe := target.Pipeline()
		hsRes := make([]*redis.IntCmd, len(wcmds))
		for i, c := range wcmds {
			hsRes[i] = pipe.HSet(ctx, c.key, c.fields)
		}
		delRes := make([]*redis.IntCmd, len(dcmds))
		for i, d := range dcmds {
			delRes[i] = pipe.Del(ctx, d.key)
		}
		// Capture the aggregate pipeline error — do NOT ignore it. If the
		// pipeline itself failed to execute cleanly (connection drop, context
		// cancellation on shutdown/scale-in, or a partial cluster failure), an
		// individual command's Err() can be nil even though the write never
		// durably landed in the target. Acking on that optimistic nil is exactly
		// how a change gets silently dropped (observed: 1 row acked-but-unwritten
		// during a sink scale-in, and retry/DLQ can't help an already-acked entry).
		_, execErr := pipe.Exec(ctx)
		t5 := time.Now()
		writeDur := t5.Sub(t4)

		// STEP 4: ACK + trim ONLY entries whose write we can CONFIRM landed in
		// the target. Confirmation requires BOTH a clean pipeline execution
		// (execErr == nil) AND a nil per-command error. Anything unconfirmed is
		// left in the pending list (PEL) for reclaimLoop() to replay — never
		// acked, never deleted. Writes are idempotent (keyed by table:pk), so a
		// replay is a harmless no-op. This is the confirmed-apply-before-trim
		// invariant: a broker entry is removed only once the target accepted the
		// write. (Durability AFTER the target accepts is the target's job.)
		t6 := time.Now()
		ackIDs := make([]string, 0, batchLen)
		okW := 0
		okD := 0
		if execErr == nil {
			for i, c := range wcmds {
				if hsRes[i].Err() == nil {
					ackIDs = append(ackIDs, c.id)
					okW++
				}
			}
			for i, d := range dcmds {
				if delRes[i].Err() == nil {
					ackIDs = append(ackIDs, d.id)
					okD++
				}
			}
		}
		// Poison messages are structurally invalid and never entered the target
		// pipeline, so their handling is independent of execErr: dead-letter them
		// (a durable copy in the DLQ stream) and then ack the originals.
		for _, m := range poison {
			toDLQ(ctx, broker, m, stream, "poison", 0)
			ackIDs = append(ackIDs, m.ID)
		}
		if len(poison) > 0 {
			atomic.AddInt64(&dlqSent, int64(len(poison)))
		}
		if len(ackIDs) > 0 {
			broker.XAck(ctx, stream, group, ackIDs...)
			broker.XDel(ctx, stream, ackIDs...)
		}
		t7 := time.Now()
		ackDur := t7.Sub(t6)

		// Record timings + counters (count only successful writes/deletes)
		atomic.AddInt64(&totalRead, int64(readDur))
		atomic.AddInt64(&totalParse, int64(parseDur))
		atomic.AddInt64(&totalWrite, int64(writeDur))
		atomic.AddInt64(&totalAck, int64(ackDur))
		atomic.AddInt64(&cycles, 1)
		atomic.AddInt64(&batchSizes, batchLen)
		atomic.AddInt64(&writes, int64(okW))
		atomic.AddInt64(&deletes, int64(okD))
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// startHealthServer exposes /health (liveness) and /metrics (counters) so ECS
// can health-check the container and monitoring can scrape progress.
func startHealthServer(port string) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ok")
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"writes":%d,"deletes":%d,"cycles":%d,"dlq":%d,"retried":%d}`+"\n",
			atomic.LoadInt64(&writes), atomic.LoadInt64(&deletes), atomic.LoadInt64(&cycles),
			atomic.LoadInt64(&dlqSent), atomic.LoadInt64(&retried))
	})
	srv := &http.Server{Addr: ":" + port, Handler: mux}
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			fmt.Printf("  health server error: %v\n", err)
		}
	}()
	fmt.Printf("  Health server on :%s\n", port)
}

// pkFromMsg derives the row identity from the Debezium record key (the table's
// primary key), supporting single and composite keys. Falls back to the row's
// "id" field if the key field is absent.
func pkFromMsg(msg redis.XMessage, row map[string]interface{}) string {
	if k, ok := msg.Values["key"].(string); ok && k != "" {
		var keyObj map[string]interface{}
		if err := decodeJSON([]byte(k), &keyObj); err == nil && len(keyObj) > 0 {
			if len(keyObj) == 1 {
				for _, v := range keyObj {
					return fmt.Sprintf("%v", v)
				}
			}
			// Composite key: join values in a deterministic (sorted) column order.
			names := make([]string, 0, len(keyObj))
			for name := range keyObj {
				names = append(names, name)
			}
			sort.Strings(names)
			parts := make([]string, 0, len(names))
			for _, name := range names {
				parts = append(parts, fmt.Sprintf("%v", keyObj[name]))
			}
			return strings.Join(parts, ":")
		}
	}
	// Fallback (single-PK convenience): use row's id if present.
	if v, ok := row["id"]; ok {
		return fmt.Sprintf("%v", v)
	}
	return ""
}

// ─────────────────────────────────────────────────────────────────────────
// Denormalization worker (FAN-OUT: parent -> children).
// Consumes parent + child streams; embeds parent fields onto children via a
// cache lookup (Phase 1); maintains a reverse index and fans parent updates
// out to children, diff-gated + batched (Phase 2). Correctness-first: each
// message is processed individually rather than via the batched fast path.
// (This does NOT do fan-in / child->parent aggregation — that's a separate
// feature.)
// ─────────────────────────────────────────────────────────────────────────

func specHasDenorm(s *TransformSpec) bool {
	if s == nil {
		return false
	}
	for _, ts := range s.Tables {
		if len(ts.Denormalize) > 0 {
			return true
		}
	}
	return false
}

func idxKey(childTable, parentTable, parentPK string) string {
	return "idx:" + childTable + ":by:" + parentTable + ":" + parentPK
}

func toIface(m map[string]string) map[string]interface{} {
	out := make(map[string]interface{}, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// embeddedChanged reports whether any embedded (parent) field differs between
// the before- and after-images — used to skip needless fan-out.
func embeddedChanged(rule DenormRule, before, after map[string]string) bool {
	for _, f := range rule.Fields {
		if before[f] != after[f] {
			return true
		}
	}
	return false
}

func workerDenorm(ctx context.Context, broker, target redis.UniversalClient, streams []string, group, name string, batch int64) {
	// Multi-stream XREADGROUP arg: [s1,s2,...,">",">",...]
	sarg := make([]string, 0, len(streams)*2)
	sarg = append(sarg, streams...)
	for range streams {
		sarg = append(sarg, ">")
	}
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		results, err := broker.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group: group, Consumer: name, Streams: sarg, Count: batch, Block: 500 * time.Millisecond,
		}).Result()
		if err != nil || len(results) == 0 {
			continue
		}
		for _, st := range results {
			ackIDs := make([]string, 0, len(st.Messages))
			for _, msg := range st.Messages {
				processMsg(ctx, target, msg)
				ackIDs = append(ackIDs, msg.ID)
			}
			if len(ackIDs) > 0 {
				broker.XAck(ctx, st.Stream, group, ackIDs...)
				broker.XDel(ctx, st.Stream, ackIDs...)
			}
		}
	}
}

func processMsg(ctx context.Context, target redis.UniversalClient, msg redis.XMessage) {
	val, ok := msg.Values["value"].(string)
	if !ok {
		return
	}
	var evt CDCEvent
	if err := decodeJSON([]byte(val), &evt); err != nil {
		return
	}
	table := evt.Source.Table
	spec := transformSpec

	// ── DELETE ──────────────────────────────────────────────────────────
	if evt.Op == "d" {
		pk := pkFromMsg(msg, evt.Before)
		if pk == "" {
			return
		}
		target.Del(ctx, table+":"+pk)
		atomic.AddInt64(&deletes, 1)
		if evt.Before != nil { // as CHILD: drop from parent index
			before := stringifyRow(evt.Before)
			for _, r := range spec.DenormFor(table) {
				if ppk := before[r.JoinField]; ppk != "" {
					target.SRem(ctx, idxKey(table, r.ParentTable, ppk), pk)
				}
			}
		}
		for _, cd := range spec.ChildrenOf(table) { // as PARENT: apply delete policy
			applyParentDelete(ctx, target, cd, pk)
		}
		return
	}

	if evt.After == nil {
		return
	}
	pk := pkFromMsg(msg, evt.After)
	after := stringifyRow(evt.After)
	key := table + ":" + pk

	out, keep := spec.Apply(table, after)
	if !keep {
		target.Del(ctx, key)
		atomic.AddInt64(&deletes, 1)
		if evt.Before != nil {
			before := stringifyRow(evt.Before)
			for _, r := range spec.DenormFor(table) {
				if ppk := before[r.JoinField]; ppk != "" {
					target.SRem(ctx, idxKey(table, r.ParentTable, ppk), pk)
				}
			}
		}
		return
	}

	// Phase 1: embed parent fields (this table as CHILD) via cache lookup.
	for _, r := range spec.DenormFor(table) {
		ppk := after[r.JoinField]
		if ppk == "" {
			continue
		}
		vals, err := target.HMGet(ctx, r.ParentKeyPrefix+ppk, r.Fields...).Result()
		parent := make(map[string]string, len(r.Fields))
		if err == nil {
			for i, f := range r.Fields {
				if i < len(vals) && vals[i] != nil {
					parent[f] = fmt.Sprintf("%v", vals[i])
				}
			}
		}
		for k, v := range EmbedParentFields(r, parent) {
			out[k] = v
		}
	}

	target.HSet(ctx, key, toIface(out))
	atomic.AddInt64(&writes, 1)

	// Phase 2: reverse-index maintenance (this table as CHILD).
	for _, r := range spec.DenormFor(table) {
		ppk := after[r.JoinField]
		if ppk != "" {
			target.SAdd(ctx, idxKey(table, r.ParentTable, ppk), pk)
		}
		if evt.Before != nil {
			old := stringifyRow(evt.Before)[r.JoinField]
			if old != "" && old != ppk {
				target.SRem(ctx, idxKey(table, r.ParentTable, old), pk)
			}
		}
	}

	// Phase 2: parent-update fan-out (this table as PARENT).
	children := spec.ChildrenOf(table)
	if len(children) == 0 {
		return
	}
	var before map[string]string
	if evt.Before != nil {
		before = stringifyRow(evt.Before)
	}
	for _, cd := range children {
		if before != nil && !embeddedChanged(cd.Rule, before, after) {
			continue // diff-gate: embedded fields unchanged
		}
		embed := toIface(EmbedParentFields(cd.Rule, after))
		members, err := target.SMembers(ctx, idxKey(cd.ChildTable, table, pk)).Result()
		if err != nil || len(members) == 0 {
			continue
		}
		pipe := target.Pipeline()
		for _, cpk := range members {
			pipe.HSet(ctx, cd.ChildTable+":"+cpk, embed)
		}
		pipe.Exec(ctx)
		atomic.AddInt64(&writes, int64(len(members)))
	}
}

// applyParentDelete handles a parent row deletion for one child relationship:
// "null" blanks the embedded fields on every child; "leave" does nothing.
func applyParentDelete(ctx context.Context, target redis.UniversalClient, cd ChildDenorm, parentPK string) {
	if cd.Rule.OnParentDelete == "leave" {
		return
	}
	members, err := target.SMembers(ctx, idxKey(cd.ChildTable, cd.Rule.ParentTable, parentPK)).Result()
	if err != nil || len(members) == 0 {
		return
	}
	blank := make(map[string]interface{}, len(cd.Rule.Fields))
	for _, f := range cd.Rule.Fields {
		blank[cd.Rule.Prefix+f] = ""
	}
	pipe := target.Pipeline()
	for _, cpk := range members {
		pipe.HSet(ctx, cd.ChildTable+":"+cpk, blank)
	}
	pipe.Exec(ctx)
}
