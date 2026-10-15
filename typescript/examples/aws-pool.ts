import { Arker } from "@arker-ai/sdk";

const client = new Arker({ provider: "aws", region: "us-west-2" });
const accountId = process.env.AWS_POOL_ACCOUNT_ID;
const subnetId = process.env.AWS_POOL_SUBNET_ID;
if (!accountId || !subnetId) throw new Error("Set AWS_POOL_ACCOUNT_ID and AWS_POOL_SUBNET_ID");

const poolId = process.env.ARKER_POOL_ID;
if (!poolId) {
  const pool = await client.createPool({
    type: "aws",
    name: "ci",
    platform: "icelake",
    resources: { vcpu: 8, memory_mib: 32768, disk_mib: 102400 },
    aws: { account_id: accountId, subnet_ids: [subnetId] },
  });
  if (pool.type !== "aws") throw new Error(`Unexpected pool type: ${pool.type}`);
  console.log(`Open ${pool.aws.setup_url} signed in to the pool's AWS account and create the stack.`);
  console.log(`Run this example again with ARKER_POOL_ID=${pool.pool_id} to inspect readiness and fork.`);
} else {
  const pool = await client.getPool(poolId);
  if (pool.state !== "ready") {
    console.log(`${pool.state}: ${pool.state_detail ?? "Waiting for hosts to join"}`);
  } else {
    const vm = await client.fork({ image: "ubuntu", pool_id: pool.pool_id });
    console.log(`Forked ${vm.id}; use run for commands (pool VMs have no interactive shells).`);
  }
}
