package main

// CDC-cache periodic aggregator (fan-in): compute a rollup over each parent's child
// rows and flatten the result onto the PARENT hash, so the app reads one key
// (HGETALL customers:{id}) with the score inline — no joins, no fan-in on the
// CDC hot path. Meant to run on a schedule (cron / ECS scheduled task).
//
// Uses the reverse index the sink maintains: idx:{child}:by:{parent}:{parent_pk}
// = set of child ids. For each parent: read its children's fields, aggregate,
// evaluate a score expression, and HSET the results onto the parent.
//
// Env:
//   TARGET_ADDR (clustercfg host:port), TARGET_SSL, TARGET_CLUSTER
//   INDEX_PREFIX   (default idx:orders:by:customers:)
//   CHILD_PREFIX   (default orders:)
//   PARENT_PREFIX  (default customers:)
//   SUM_FIELD      (child numeric field to sum/max, default amount)
//   SCORE_FIELD    (output field on parent, default risk_score)
//   SCORE_EXPR     (default "total_amount / (order_count + 1)")
//                  vars: order_count, total_amount, max_amount
//   WORKERS        (default 16)
import (
	"context"
	"crypto/tls"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

var parents, childReads int64

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func main() {
	addr := env("TARGET_ADDR", "localhost:6379")
	ssl := env("TARGET_SSL", "true") == "true"
	cluster := env("TARGET_CLUSTER", "true") == "true"
	indexPrefix := env("INDEX_PREFIX", "idx:orders:by:customers:")
	childPrefix := env("CHILD_PREFIX", "orders:")
	parentPrefix := env("PARENT_PREFIX", "customers:")
	sumField := env("SUM_FIELD", "amount")
	scoreField := env("SCORE_FIELD", "risk_score")
	scoreExpr := env("SCORE_EXPR", "total_amount / (order_count + 1)")
	workers, _ := strconv.Atoi(env("WORKERS", "16"))

	ctx := context.Background()
	var tlsConf *tls.Config
	if ssl {
		tlsConf = &tls.Config{InsecureSkipVerify: true}
	}
	var client redis.UniversalClient
	if cluster {
		client = redis.NewClusterClient(&redis.ClusterOptions{
			Addrs: []string{addr}, PoolSize: workers + 4,
			ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second, TLSConfig: tlsConf,
		})
	} else {
		client = redis.NewClient(&redis.Options{
			Addr: addr, PoolSize: workers + 4, TLSConfig: tlsConf,
			ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second,
		})
	}
	if err := client.Ping(ctx).Err(); err != nil {
		fmt.Println("target ping:", err)
		os.Exit(1)
	}

	start := time.Now()
	keys := scanKeys(ctx, client, indexPrefix+"*")
	fmt.Printf("aggregator: %d parents to recompute\n", len(keys))

	ch := make(chan string, 1024)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for key := range ch {
				processParent(ctx, client, key, indexPrefix, childPrefix, parentPrefix, sumField, scoreField, scoreExpr)
			}
		}()
	}
	for _, k := range keys {
		ch <- k
	}
	close(ch)
	wg.Wait()

	el := time.Since(start).Seconds()
	fmt.Printf("=== AGGREGATOR CYCLE: %d parents in %.2fs = %.0f parents/sec (%d child reads) ===\n",
		atomic.LoadInt64(&parents), el, float64(atomic.LoadInt64(&parents))/el, atomic.LoadInt64(&childReads))
}

func processParent(ctx context.Context, client redis.UniversalClient, indexKey,
	indexPrefix, childPrefix, parentPrefix, sumField, scoreField, scoreExpr string) {
	cid := strings.TrimPrefix(indexKey, indexPrefix)
	members, err := client.SMembers(ctx, indexKey).Result()
	if err != nil {
		return
	}
	pipe := client.Pipeline()
	cmds := make([]*redis.StringCmd, len(members))
	for i, oid := range members {
		cmds[i] = pipe.HGet(ctx, childPrefix+oid, sumField)
	}
	pipe.Exec(ctx)

	var sum, mx float64
	count := float64(len(members))
	for _, c := range cmds {
		v, e := c.Result()
		if e != nil {
			continue
		}
		f := toFloat(v)
		sum += f
		if f > mx {
			mx = f
		}
	}
	vars := map[string]float64{"order_count": count, "total_amount": sum, "max_amount": mx}
	score := evalExpr(scoreExpr, vars)

	client.HSet(ctx, parentPrefix+cid,
		"order_count", fmtNum(count),
		"total_amount", fmtNum(sum),
		"max_amount", fmtNum(mx),
		scoreField, fmtNum(score))
	atomic.AddInt64(&parents, 1)
	atomic.AddInt64(&childReads, int64(len(members)))
}

// scanKeys walks all shards (cluster) or the single node for keys matching a pattern.
func scanKeys(ctx context.Context, uc redis.UniversalClient, match string) []string {
	var keys []string
	var mu sync.Mutex
	scanNode := func(ctx context.Context, c *redis.Client) error {
		var cursor uint64
		for {
			ks, cur, err := c.Scan(ctx, cursor, match, 1000).Result()
			if err != nil {
				return err
			}
			mu.Lock()
			keys = append(keys, ks...)
			mu.Unlock()
			cursor = cur
			if cursor == 0 {
				return nil
			}
		}
	}
	if cc, ok := uc.(*redis.ClusterClient); ok {
		cc.ForEachMaster(ctx, scanNode)
	} else if c, ok := uc.(*redis.Client); ok {
		scanNode(ctx, c)
	}
	return keys
}

func toFloat(s string) float64 {
	f, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil {
		return 0
	}
	return f
}

func fmtNum(v float64) string {
	s := strconv.FormatFloat(v, 'f', 6, 64)
	if strings.Contains(s, ".") {
		s = strings.TrimRight(strings.TrimRight(s, "0"), ".")
	}
	return s
}

// ── minimal arithmetic evaluator: + - * / , parens, numbers, identifiers ──
type ep struct {
	s    string
	pos  int
	vars map[string]float64
}

func evalExpr(expr string, vars map[string]float64) float64 {
	p := &ep{s: expr, vars: vars}
	return p.expr()
}
func (p *ep) sp() {
	for p.pos < len(p.s) && (p.s[p.pos] == ' ' || p.s[p.pos] == '\t') {
		p.pos++
	}
}
func (p *ep) expr() float64 {
	v := p.term()
	for {
		p.sp()
		if p.pos >= len(p.s) {
			break
		}
		switch p.s[p.pos] {
		case '+':
			p.pos++
			v += p.term()
		case '-':
			p.pos++
			v -= p.term()
		default:
			return v
		}
	}
	return v
}
func (p *ep) term() float64 {
	v := p.factor()
	for {
		p.sp()
		if p.pos >= len(p.s) {
			break
		}
		switch p.s[p.pos] {
		case '*':
			p.pos++
			v *= p.factor()
		case '/':
			p.pos++
			d := p.factor()
			if d != 0 {
				v /= d
			} else {
				v = 0
			}
		default:
			return v
		}
	}
	return v
}
func (p *ep) factor() float64 {
	p.sp()
	if p.pos >= len(p.s) {
		return 0
	}
	c := p.s[p.pos]
	if c == '(' {
		p.pos++
		v := p.expr()
		p.sp()
		if p.pos < len(p.s) && p.s[p.pos] == ')' {
			p.pos++
		}
		return v
	}
	if c == '-' {
		p.pos++
		return -p.factor()
	}
	if (c >= '0' && c <= '9') || c == '.' {
		st := p.pos
		for p.pos < len(p.s) && ((p.s[p.pos] >= '0' && p.s[p.pos] <= '9') || p.s[p.pos] == '.') {
			p.pos++
		}
		f, _ := strconv.ParseFloat(p.s[st:p.pos], 64)
		return f
	}
	st := p.pos
	for p.pos < len(p.s) {
		ch := p.s[p.pos]
		if (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') || ch == '_' {
			p.pos++
		} else {
			break
		}
	}
	if p.pos == st {
		p.pos++
		return 0
	}
	return p.vars[p.s[st:p.pos]]
}
