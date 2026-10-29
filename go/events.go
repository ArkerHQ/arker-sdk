package arker

import (
	"context"
	"strconv"
)

// EventActor is who initiated an action, and how. Via is "console", "api",
// "ssh" or "arker"; ID is a member's email, an API key's id or an SSH key's
// id, and nil when Arker acted.
type EventActor struct {
	Via string  `json:"via"`
	ID  *string `json:"id"`
}

// Event is one action in the organization's activity. A nil field is one
// the action does not have, or does not have yet.
type Event struct {
	ID          string         `json:"id"`
	Action      string         `json:"action"`
	Category    string         `json:"category"` // "compute" or "organization"
	State       string         `json:"state"`
	VMID        *string        `json:"vm_id"`
	RequestedAt string         `json:"requested_at"`
	StartedAt   *string        `json:"started_at"`
	EndedAt     *string        `json:"ended_at"`
	DurationMS  *int64         `json:"duration_ms"`
	Actor       *EventActor    `json:"actor"`
	Region      *string        `json:"region"`
	RequestID   *string        `json:"request_id"`
	ErrorCode   *string        `json:"error_code"`
	Attributes  map[string]any `json:"attributes"`
}

// EventList is one page of events.
type EventList struct {
	Events     []Event `json:"events"`
	NextCursor *string `json:"next_cursor"`
}

// TimeRange bounds an event's time, RFC 3339: at or after GTE, before LT.
// An empty end is open.
type TimeRange struct {
	GTE string
	LT  string
}

// DurationRange bounds an event's duration in milliseconds; a nil end is open.
type DurationRange struct {
	GTE *int64
	LT  *int64
}

// EventFilter picks the events a page lists. Values of one field match any
// of them; fields combine.
type EventFilter struct {
	Search      string // text in an event's action, ids, error code, attributes or member's email
	Category    string // "compute" or "organization"; empty for every event the caller can read
	Actions     []string
	States      []string
	Regions     []string
	VMIDs       []string
	Actors      []string // member emails or "me", API key ids, SSH key ids
	Via         []string // "console", "api", "ssh" or "arker"
	RequestIDs  []string
	ErrorCodes  []string
	ExitCode    *int
	RequestedAt TimeRange
	StartedAt   TimeRange
	EndedAt     TimeRange
	DurationMS  DurationRange
}

// ListEventsOptions selects and orders a page of events. Cursor is a
// previous page's NextCursor; it carries that page's filter and order.
type ListEventsOptions struct {
	Filter  EventFilter
	OrderBy string
	Order   string
	Limit   int
	Cursor  string
}

// ListEvents lists the organization's activity across regions, one event per
// action.
func (c *Client) ListEvents(ctx context.Context, opts ListEventsOptions) (*EventList, error) {
	f := opts.Filter
	q := newQuery()
	q.str("search", f.Search)
	q.str("category", f.Category)
	q.each("actions", f.Actions)
	q.each("states", f.States)
	q.each("regions", f.Regions)
	q.each("vm_ids", f.VMIDs)
	q.each("actors", f.Actors)
	q.each("via", f.Via)
	q.each("request_ids", f.RequestIDs)
	q.each("error_codes", f.ErrorCodes)
	q.numPtr("exit_code", f.ExitCode)
	for name, r := range map[string]TimeRange{
		"requested_at": f.RequestedAt,
		"started_at":   f.StartedAt,
		"ended_at":     f.EndedAt,
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
	var out EventList
	_, err := c.control(ctx, q.on("/v1/events"), &out)
	return &out, err
}
