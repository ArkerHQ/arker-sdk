#!/usr/bin/env python3
# ---------------- HYPERPARAMS (the agent edits these) ----------------
LR = 1e-4
D_MODEL = 32
N_LAYER = 1
N_HEAD = 2
STEPS = 200
BATCH = 32
# ---------------------------------------------------------------------
import os, time
import torch, torch.nn as nn, torch.nn.functional as F

torch.manual_seed(1234)
dev = "cuda" if torch.cuda.is_available() else "cpu"
SEQ, VOCAB = 96, 48

# Multi-rule copy: the CURRENT token decides which lag to copy from, so the model
# must learn four different offsets and switch between them. Partial mastery earns
# partial credit - getting 1 of the 4 rules right lands near 3.1, two near 2.3,
# three near 1.5, all four near the 0.70 floor - so the search descends in steps
# over many turns instead of hitting the floor on turn 2. Chance is ln(48) = 3.87.
LAGS, SIGNAL = (4, 11, 23, 37), 0.9
MAXLAG = max(LAGS)
g = torch.Generator().manual_seed(7)
def make(n):
    x = torch.randint(0, VOCAB, (n, SEQ), generator=g)
    y = torch.zeros_like(x)
    rule = x % len(LAGS)                      # the token at t picks the offset
    for r, lag in enumerate(LAGS):
        src = torch.zeros_like(x)
        src[:, lag:] = x[:, :-lag]
        y = torch.where(rule == r, src, y)
    noise = torch.rand(n, SEQ, generator=g) > SIGNAL
    rand = torch.randint(0, VOCAB, (n, SEQ), generator=g)
    y = torch.where(noise, rand, y)
    return x, y
train_x, train_y = make(8192)
val_x, val_y = make(512)

class Block(nn.Module):
    def __init__(s, d, h):
        super().__init__()
        s.at = nn.MultiheadAttention(d, h, batch_first=True)
        s.n1, s.n2 = nn.LayerNorm(d), nn.LayerNorm(d)
        s.ff = nn.Sequential(nn.Linear(d, 4 * d), nn.GELU(), nn.Linear(4 * d, d))
    def forward(s, x):
        h = s.n1(x)
        m = torch.triu(torch.ones(x.size(1), x.size(1), device=x.device, dtype=torch.bool), 1)
        a, _ = s.at(h, h, h, attn_mask=m, need_weights=False)
        x = x + a
        return x + s.ff(s.n2(x))

class Model(nn.Module):
    def __init__(s):
        super().__init__()
        s.emb = nn.Embedding(VOCAB, D_MODEL)
        s.pos = nn.Parameter(torch.zeros(1, SEQ, D_MODEL))
        s.blocks = nn.ModuleList([Block(D_MODEL, N_HEAD) for _ in range(N_LAYER)])
        s.norm = nn.LayerNorm(D_MODEL)
        s.head = nn.Linear(D_MODEL, VOCAB)
    def forward(s, idx):
        x = s.emb(idx) + s.pos[:, : idx.size(1)]
        for b in s.blocks: x = b(x)
        return s.head(s.norm(x))

model = Model().to(dev)
opt = torch.optim.AdamW(model.parameters(), lr=LR)
t0 = time.time()
for step in range(STEPS):
    i = torch.randint(0, train_x.size(0), (BATCH,))
    xb, yb = train_x[i].to(dev), train_y[i].to(dev)
    logits = model(xb)
    loss = F.cross_entropy(logits[:, MAXLAG:].reshape(-1, VOCAB), yb[:, MAXLAG:].reshape(-1))
    opt.zero_grad(set_to_none=True); loss.backward(); opt.step()

model.eval()
with torch.no_grad():
    xb, yb = val_x.to(dev), val_y.to(dev)
    logits = model(xb)
    val = F.cross_entropy(logits[:, MAXLAG:].reshape(-1, VOCAB), yb[:, MAXLAG:].reshape(-1)).item()
secs = time.time() - t0
params = sum(p.numel() for p in model.parameters())

row = f"{time.strftime('%H:%M:%S')}\t{LR:g}\t{D_MODEL}\t{N_LAYER}\t{N_HEAD}\t{STEPS}\t{BATCH}\t{params}\t{val:.4f}\t{secs:.1f}"
hdr = "time\tlr\td_model\tn_layer\tn_head\tsteps\tbatch\tparams\tval_loss\tsecs"
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.tsv")
if not os.path.exists(p): open(p, "w").write(hdr + "\n")
open(p, "a").write(row + "\n")
print(f"val_loss {val:.4f}  ({secs:.1f}s, {params} params, dev={dev})")
