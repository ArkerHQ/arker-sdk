package arker

import (
	"context"
	"strconv"
)

// LogActor is who initiated an action: a member by email, or an API key by
// its id.
type LogActor struct {
	Type string `json:"type"`
	ID   string `json:"id"`
}

// LogEntry is one action in the organization's activity. A nil field is one
// the action does not have, or does not have yet.
type LogEntry struct {
	ID          string         `json:"id"`
	Action      string         `json:"action"`
	Outcome     string         `json:"outcome"`
	VMID        *string        `json:"vm_id"`
	RequestedAt string         `json:"requested_at"`
	StartedAt   *string        `json:"started_at"`
	CompletedAt *string        `json:"completed_at"`
	DurationMS  *int64         `json:"duration_ms"`
	Actor       *LogActor      `json:"actor"`
	Region      *string        `json:"region"`
	RequestID   *string        `json:"request_id"`
	ErrorCode   *string        `json:"error_code"`
	Attributes  map[string]any `json:"attributes"`
}

// LogList is one page of entries.
type LogList struct {
	Logs       []LogEntry `json:"logs"`
	NextCursor *string    `json:"next_cursor"`
}

// TimeRange bounds an entry's time, RFC 3339: at or after GTE, before LT.
// An empty end is open.
type TimeRange struct {
	GTE string
	LT  string
}

// DurationRange bounds an entry's duration in milliseconds; a nil end is open.
type DurationRange struct {
	GTE *int64
	LT  *int64
}

// LogFilter picks the entries a page lists. Values of one field match any of
// them; fields combine, except Members and Keys, either of which matches.
type LogFilter struct {
	Actions     []string
	Outcomes    []string
	Regions     []string
	VMIDs       []string
	Members     []string // member emails, or "me"
	Keys        []string // API key ids
	RequestIDs  []string
	ErrorCodes  []string
	ExitCode    *int
	RequestedAt TimeRange
	StartedAt   TimeRange
	CompletedAt TimeRange
	DurationMS  DurationRange
}

// ListLogsOptions selects and orders a page of logs. Cursor is a previous
// page's NextCursor; it carries that page's filter and order.
type ListLogsOptions struct {
	Filter  LogFilter
	OrderBy string
	Order   string
	Limit   int
	Cursor  string
}

// ListLogs lists the organization's activity across regions, one entry per
// action.
func (c *Client) ListLogs(ctx context.Context, opts ListLogsOptions) (*LogList, error) {
	f := opts.Filter
	q := newQuery()
	q.each("actions", f.Actions)
	q.each("outcomes", f.Outcomes)
	q.each("regions", f.Regions)
	q.each("vm_ids", f.VMIDs)
	q.each("members", f.Members)
	q.each("keys", f.Keys)
	q.each("request_ids", f.RequestIDs)
	q.each("error_codes", f.ErrorCodes)
	q.numPtr("exit_code", f.ExitCode)
	for name, r := range map[string]TimeRange{
		"requested_at": f.RequestedAt,
		"started_at":   f.StartedAt,
		"completed_at": f.CompletedAt,
	} {
		q.str(name+"_gte", r.GTE)
		q.str(name+"_lt", r.LT)
	}
	if f.DurationMS.GTE != nil {
		q.str("duration_ms_gte", strconv.FormatInt(*f.DurationMS.GTE, 10))
	}
	if f.DurationMS.LT != nil {
		q.str("duration_ms_lt", strconv.FormatInt(*f.DurationMS.LT, 10))
	}
	q.str("order_by", opts.OrderBy)
	q.str("order", opts.Order)
	q.num("limit", opts.Limit)
	q.str("cursor", opts.Cursor)
	var out LogList
	_, err := c.control(ctx, q.on("/v1/logs"), &out)
	return &out, err
}
