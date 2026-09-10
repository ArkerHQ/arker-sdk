package arker

import (
	"context"
	"net/http"
)

// RegionPlacement is one public provider/region pair and the endpoint serving
// it.
type RegionPlacement struct {
	Provider string `json:"provider"`
	Region   string `json:"region"`
	Endpoint string `json:"endpoint"`
}

// Whoami identifies the organization behind the credentials.
type Whoami struct {
	OrgID   string `json:"org_id"`
	OrgName string `json:"org_name"`
}

// ListRegions lists the available public placements.
func (c *Client) ListRegions(ctx context.Context) ([]RegionPlacement, error) {
	var out struct {
		Regions []RegionPlacement `json:"regions"`
	}
	_, err := c.control(ctx, "/v1/regions", &out)
	return out.Regions, err
}

// Whoami returns the organization these credentials belong to.
func (c *Client) Whoami(ctx context.Context) (*Whoami, error) {
	var out Whoami
	_, err := c.control(ctx, "/v1/whoami", &out)
	return &out, err
}

// DiscoverRegions reads the public placement catalog with no credentials and no
// placement configured. controlBaseURL may be empty for the default.
func DiscoverRegions(ctx context.Context, controlBaseURL string) ([]RegionPlacement, error) {
	c := &Client{
		http:  defaultHTTPClient(),
		retry: Retry{defaultAttempts, defaultBaseDelay, defaultMaxDelay, defaultJitter},
	}
	base := trimURL(firstNonEmpty(controlBaseURL, DefaultControlBaseURL))
	var out struct {
		Regions []RegionPlacement `json:"regions"`
	}
	_, err := c.do(ctx, call{method: http.MethodGet, path: "/v1/regions", base: base, out: &out})
	return out.Regions, err
}
