// Linear-time slash trimming. A regex like `/\/+$/` backtracks at every run of
// `/` that is not at the end, so a long run of slashes followed by any other
// character makes it quadratic on caller-supplied paths and URLs.

export function trimTrailingSlashes(value: string): string {
  let end = value.length;
  while (end > 0 && value.charCodeAt(end - 1) === 47 /* "/" */) end--;
  return end === value.length ? value : value.slice(0, end);
}

export function trimLeadingSlashes(value: string): string {
  let start = 0;
  while (start < value.length && value.charCodeAt(start) === 47 /* "/" */) start++;
  return start === 0 ? value : value.slice(start);
}

export function trimSlashes(value: string): string {
  return trimTrailingSlashes(trimLeadingSlashes(value));
}
