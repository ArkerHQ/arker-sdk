/** Run a named part of a live test; `LIVE_ONLY=<name>` runs only the parts whose names contain it. */
export async function section(name: string, body: () => Promise<void>): Promise<void> {
  const only = process.env.LIVE_ONLY;
  if (only && !name.includes(only)) return;
  await body();
  console.log(`ok - ${name}`);
}
