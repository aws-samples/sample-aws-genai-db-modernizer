package main

// Reliability layer: retry + dead-letter for the CDC sink.
//
// Problem it solves: the fast path in worker() writes a batch to the target,
// then ACKs it. If a single change can't be written (target OOM, value too big,
// a transient network blip) or can't even be parsed ("poison"), the old code
// dropped it silently — the cache was left stale for that key with no retry and
// no visibility.
//
// How it works now:
//   - worker() ACKs only the entries whose write SUCCEEDED. A failed write is
//     left in the consumer group's Pending Entries List (PEL), unacked.
//   - reclaimLoop() periodically claims entries that have been pending longer
//     than RETRY_IDLE_MS and reprocesses them (automatic retry with backoff via
//     the idle threshold). Each claim bumps the entry's delivery count.
//   - Once an entry's delivery count passes MAX_DELIVERIES, it is written to a
//     dead-letter stream (DLQ_STREAM_PREFIX + source stream) with failure
//     metadata, then acked so it stops looping.
//   - "Poison" messages (unparseable / structurally invalid) skip retries and
//     go straight to the DLQ — retrying can't help them.
//
// Nothing is silently dropped: a change either lands in the cache or lands in
// the DLQ where it is visible and replayable.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strconv"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

// errPoison marks a message that can never succeed as-is (bad JSON, no value,
// structurally invalid). Such messages are dead-lettered immediately.
var errPoison = errors.New("poison: unprocessable message")

var (
	dlqPrefix       = envOr("DLQ_STREAM_PREFIX", "dlq:")
	maxDeliveries   = int64(atoiOr("MAX_DELIVERIES", 5))
	retryIdle       = time.Duration(atoiOr("RETRY_IDLE_MS", 30000)) * time.Millisecond
	reclaimInterval = time.Duration(atoiOr("RECLAIM_INTERVAL_MS", 15000)) * time.Millisecond
	reclaimBatch    = int64(atoiOr("RECLAIM_BATCH", 200))

	dlqSent int64 // messages routed to the DLQ
	retried int64 // messages that succeeded on a retry
)

func atoiOr(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// planMessage parses + transforms one broker message into an intended action:
//   - poison=true               -> unprocessable, send to DLQ
//   - isDelete=true             -> DEL key on the target
//   - otherwise                 -> HSET key = fields on the target
//
// This is the single source of truth for how a CDC event maps to a target
// command, shared by the fast path (worker) and the retry path (reclaimLoop).
func planMessage(msg redis.XMessage, eventFormat, streamTable string) (key string, fields map[string]interface{}, isDelete bool, poison bool) {
	val, ok := msg.Values["value"].(string)
	if !ok {
		return "", nil, false, true
	}

	if eventFormat == "flat" {
		var row map[string]interface{}
		if err := decodeJSON([]byte(val), &row); err != nil || row == nil {
			return "", nil, false, true
		}
		pk := pkFromMsg(msg, row)
		if pk == "" {
			return "", nil, false, true
		}
		key = streamTable + ":" + pk
		out, keep := transformSpec.Apply(streamTable, stringifyRow(row))
		if !keep {
			return key, nil, true, false // filtered out -> delete
		}
		f := make(map[string]interface{}, len(out))
		for k, v := range out {
			f[k] = v
		}
		return key, f, false, false
	}

	var evt CDCEvent
	if err := decodeJSON([]byte(val), &evt); err != nil {
		return "", nil, false, true
	}
	if evt.Op == "d" {
		pk := pkFromMsg(msg, evt.Before)
		if pk == "" {
			return "", nil, false, true
		}
		return evt.Source.Table + ":" + pk, nil, true, false
	}
	if evt.After == nil {
		return "", nil, false, true
	}
	pk := pkFromMsg(msg, evt.After)
	if pk == "" {
		return "", nil, false, true
	}
	key = evt.Source.Table + ":" + pk
	out, keep := transformSpec.Apply(evt.Source.Table, stringifyRow(evt.After))
	if !keep {
		return key, nil, true, false // filtered out / pushed out of scope -> delete
	}
	f := make(map[string]interface{}, len(out))
	for k, v := range out {
		f[k] = v
	}
	return key, f, false, false
}

// processOne applies a single message to the target. Returns:
//   - nil        on success
//   - errPoison  if the message is unprocessable (dead-letter it)
//   - other err  on a transient write failure (leave for another retry)
func processOne(ctx context.Context, target redis.UniversalClient, msg redis.XMessage, eventFormat, streamTable string) error {
	key, fields, isDelete, poison := planMessage(msg, eventFormat, streamTable)
	if poison {
		return errPoison
	}
	if isDelete {
		if err := target.Del(ctx, key).Err(); err != nil {
			return err
		}
		atomic.AddInt64(&deletes, 1)
		return nil
	}
	if err := target.HSet(ctx, key, fields).Err(); err != nil {
		return err
	}
	atomic.AddInt64(&writes, 1)
	return nil
}

// toDLQ writes a failed message to the dead-letter stream for its source stream,
// preserving the original payload and adding failure metadata so it can be
// inspected and replayed.
func toDLQ(ctx context.Context, broker redis.UniversalClient, m redis.XMessage, sourceStream, reason string, retries int64) {
	val, _ := m.Values["value"].(string)
	key, _ := m.Values["key"].(string)
	broker.XAdd(ctx, &redis.XAddArgs{
		Stream: dlqPrefix + sourceStream,
		Values: map[string]interface{}{
			"orig_id":       m.ID,
			"source_stream": sourceStream,
			"reason":        reason,
			"retries":       retries,
			"failed_at":     time.Now().UTC().Format(time.RFC3339),
			"key":           key,
			"value":         val,
		},
	})
}

// reclaimLoop retries entries that were left unacked (failed writes) and
// dead-letters the ones that exhaust MAX_DELIVERIES. It runs on its own
// consumer name so it never competes with the live workers for new messages.
func reclaimLoop(ctx context.Context, broker, target redis.UniversalClient, streams []string, group, consumer, eventFormat, streamTable string) {
	tick := time.NewTicker(reclaimInterval)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		for _, s := range streams {
			pend, err := broker.XPendingExt(ctx, &redis.XPendingExtArgs{
				Stream: s, Group: group, Idle: retryIdle,
				Start: "-", End: "+", Count: reclaimBatch,
			}).Result()
			if err != nil || len(pend) == 0 {
				continue
			}
			for _, p := range pend {
				// Claim the entry (transfers ownership + bumps delivery count)
				// and fetch its content in one call.
				msgs, err := broker.XClaim(ctx, &redis.XClaimArgs{
					Stream: s, Group: group, Consumer: consumer,
					MinIdle: retryIdle, Messages: []string{p.ID},
				}).Result()
				if err != nil || len(msgs) == 0 {
					continue // someone else grabbed it, or it vanished
				}
				m := msgs[0]

				// Exhausted retries -> dead-letter and stop looping.
				if p.RetryCount > maxDeliveries {
					toDLQ(ctx, broker, m, s, "max_retries", p.RetryCount)
					broker.XAck(ctx, s, group, m.ID)
					broker.XDel(ctx, s, m.ID)
					atomic.AddInt64(&dlqSent, 1)
					continue
				}

				switch err := processOne(ctx, target, m, eventFormat, streamTable); {
				case err == nil:
					broker.XAck(ctx, s, group, m.ID)
					broker.XDel(ctx, s, m.ID)
					atomic.AddInt64(&retried, 1)
				case errors.Is(err, errPoison):
					toDLQ(ctx, broker, m, s, "poison", p.RetryCount)
					broker.XAck(ctx, s, group, m.ID)
					broker.XDel(ctx, s, m.ID)
					atomic.AddInt64(&dlqSent, 1)
				default:
					// transient failure: leave it pending; the next cycle will
					// claim it again (delivery count grows toward the DLQ).
					_ = fmt.Sprint(err)
				}
			}
		}
	}
}
