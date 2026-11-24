package arker

import (
	"context"
	"net/http"
)

// VMMetrics is a VM's size, use, pressure and I/O over a time window.
type VMMetrics struct {
	VMID      string            `json:"vm_id"`
	Step      int               `json:"step"`
	StartedAt string            `json:"started_at"`
	EndedAt   string            `json:"ended_at"`
	Buckets   []VMMetricsBucket `json:"buckets"`
}

// VMMetricsBucket is one bucket in which the VM ran. Averages are over the
// time the VM ran in it; a value the VM could not report is nil.
type VMMetricsBucket struct {
	StartedAt               string                  `json:"started_at"`
	EndedAt                 string                  `json:"ended_at"`
	ResourcesProvisionedAvg VMMetricsProvisionedAvg `json:"resources_provisioned_avg"`
	ResourcesProvisionedMax VMMetricsProvisioned    `json:"resources_provisioned_max"`
	ResourcesUtilizedAvg    VMMetricsUtilized       `json:"resources_utilized_avg"`
	ResourcesUtilizedMax    VMMetricsUtilized       `json:"resources_utilized_max"`
	ResourcesPressureAvg    VMMetricsPressure       `json:"resources_pressure_avg"`
	ResourcesPressureMax    VMMetricsPressure       `json:"resources_pressure_max"`
	ResourcesIO             VMMetricsIO             `json:"resources_io"`
}

// VMMetricsProvisionedAvg is the VM's size averaged over the time it ran in
// the bucket; VCPU is fractional when a resize falls inside it.
type VMMetricsProvisionedAvg struct {
	VCPU      float64 `json:"vcpu"`
	MemoryMiB int64   `json:"memory_mib"`
	DiskMiB   int64   `json:"disk_mib"`
}

// VMMetricsProvisioned is the largest size the VM had while it ran in the
// bucket.
type VMMetricsProvisioned struct {
	VCPU      int   `json:"vcpu"`
	MemoryMiB int64 `json:"memory_mib"`
	DiskMiB   int64 `json:"disk_mib"`
}

// VMMetricsUtilized is the resources in use: the average over the time the
// VM ran, or the highest reading of about 10 s.
type VMMetricsUtilized struct {
	VCPU      *float64 `json:"vcpu"`
	MemoryMiB *int64   `json:"memory_mib"`
	DiskMiB   *int64   `json:"disk_mib"`
}

// VMMetricsPressure is the share (0-1) of running time in which some task in
// the guest waited for the resource.
type VMMetricsPressure struct {
	VCPU   *float64 `json:"vcpu"`
	Memory *float64 `json:"memory"`
}

// VMMetricsIO is the bytes the VM moved in the bucket.
type VMMetricsIO struct {
	NetworkReceivedBytes    *int64 `json:"network_received_bytes"`
	NetworkTransmittedBytes *int64 `json:"network_transmitted_bytes"`
	DiskWrittenBytes        *int64 `json:"disk_written_bytes"`
}

// VMMetricsOptions picks the window, [StartedAtGTE, StartedAtLT) in RFC 3339,
// and the bucket width in seconds: 60, 300 or 3600. Empty or zero values take
// the server's defaults: the last hour, in the smallest step that fits.
type VMMetricsOptions struct {
	StartedAtGTE string
	StartedAtLT  string
	Step         int
}

// Metrics reads this VM's usage over time. Only buckets in which the VM ran
// are returned, and history is kept for 30 days.
func (v *VM) Metrics(ctx context.Context, opts VMMetricsOptions) (*VMMetrics, error) {
	q := newQuery()
	q.str("started_at_gte", opts.StartedAtGTE)
	q.str("started_at_lt", opts.StartedAtLT)
	q.num("step", opts.Step)
	var out VMMetrics
	_, err := v.do(ctx, http.MethodGet, q.on(v.path("/metrics")), nil, &out)
	return &out, err
}
