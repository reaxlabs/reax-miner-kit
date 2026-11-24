# Inference mining v2

These rules and the model list describe the planned validator release. The start is announced at least 24 hours ahead.

## In short
- Instead of four fixed pools, every model on our list gets its own pot. You host a model, you earn from that model's pot.
- Your share of a pot follows your verified capacity, exactly as today: secret reference answers, capacity probes, strikes.
- No operator gets more than half of a pot. So every model pays a second and third operator as well as the first.
- The first operators on a newly listed model get a small, short bonus: +50 % for the first, +25 % for the second,
  +12.5 % for the third, each falling to zero within 48 hours.
- One line sets up a miner, one command switches it to another model.

## The rules
1. **One pot per listed model.** The inference part of miner emissions rises to **45 %**. It is split into one pot per listed model, weighted by the GPU class the model needs
   (96 GB 1.0, 48 GB 0.6, 24 GB 0.35, 16 GB 0.15, CPU 0.03). Later the weights may also follow customer demand per model;
   that will be announced first.
2. **Pay follows verified capacity in that model.** Same checks as today: secret reference answers, simultaneous capacity
   probes across all hotkeys of the pot, and a strike resets you to zero. Global pots keep the strict check: answers must
   match the pinned reference exactly.
3. **At most 50 % of a pot per operator, and only a full GPU unit earns that much.** An operator is everything that shares a
   coldkey, a /24 network or a multi-node gateway. Below one full unit (one GPU of the model's class at reference speed) the
   cap shrinks in proportion, so a tiny hotkey cannot claim half a pot.
4. **Pioneer bonus.** On a newly listed model (or one with no operator for 7 days), the 1st, 2nd and 3rd operator that
   qualify with at least one full unit get +50 %, +25 % and +12.5 % on their pay. Each bonus falls linearly to 0 within 48 h
   of that operator's own qualification. It is paid on top of the 50 % cap from the pot's unearned budget. Once per coldkey
   per model; at most one bonus round per model per 30 days.
5. **No redistribution.** Money a pot cannot pay (no operator, or above an operator's cap) is not handed to other miners.
   During the first 7 days it goes to the current pools (rule 7); after that it is burned. No limit on operators per pot.
6. **The list.** Models come from the top of the decision leaderboards (Hugging Face Decision Index, Artificial Analysis
   Decision Index, JevBench open-weights capability and composite), the models customers use on decisionmodels.io, and our
   own REAX model, all with a licence that allows commercial serving. Adding a model needs no notice. Removing a model is
   announced at least 24 hours ahead, and its pot keeps paying during that time.
7. **Transition for today's pools.** From the start, s1-fast, s1-lite, s1-cpu and the global s1-pro pool are paid only what
   the new pots do not yet pay out (never more than today), for 7 days. Then they end. Switching a miner to a pot model is one
   command (`reaxctl switch --model <id>`).
8. **EU operators** (under contract, data stays in the EU) are a separate layer. They must run in a TEE: confidential
   computing with attestation that our validators check. They are admitted by our answer test instead of bit-identical
   answers, so they can run as many parallel requests as their hardware answers correctly. They are paid per verified,
   attested GPU-hour, always more than the same GPU class earns in the open pots; the exact rate is in the operator
   agreement. A GPU, host or IP cannot be in both layers.
9. **Start.** The validator release with the pots starts at a time announced at least 24 hours ahead, **at the latest on
   Tuesday 1 December 2026**. Models whose check has passed are open from
   the start; the others join as soon as their check passes, without further notice.
10. **TEE for all inference mining.** TEE (confidential computing with attestation) becomes the standard in every pot,
    global and EU. GPU-hours with a valid TEE attestation count 1.5x toward your pot share as soon as validators send
    scored pot traffic to the attested node itself. Target: together with the **v2 pay start**, planned **Thursday 26
    November 2026**; the exact start is confirmed in #92. TEE verification currently covers EU sites; global sites
    follow (announced in #92). From **Thursday 3 December 2026**,
    TEE is required in every pot, provided at least two TEE operators serve each listed model on Monday 30 November; this
    is confirmed in #92 on 30 November. TEE-capable GPUs: RTX PRO 6000 Server Edition, H100, H200 or B200 with
    confidential computing on. Setup notes for TEE nodes: [TEE-OPERATOR.md](TEE-OPERATOR.md). The EU layer already
    requires a TEE (rule 8).

Nothing already earned is taken away. Rewards are variable subnet emissions and can be zero.

## Model list v1 (pots)
| Model | GPU class | At start? |
|---|---|---|
| REAX 1.3 (decisionmodels/REAX-1.3) | 96 GB | planned at start |
| d3 (vllm-sr/d3) | 96 GB | planned at start |
| Karpo Decision 31B | 96 GB | planned at start |
| Clef 27B (Cloudflare/clef) | 96 GB | planned at start |
| Blink v0.3 NVFP4 | 48 GB, Blackwell only | planned at start |
| elephant-1 (bf16) | 24 GB | planned at start |
| Torchcast Decision 4B | 16 GB | planned at start |
| Kev 4B (one request at a time) | 16 GB | planned at start |
| Laya 421M | CPU (or GPU in fp32) | planned at start |
| Torchcast Decision 31B | 96 GB | joins when its check passes |
| Perplexity Decider v1.3 | 96 GB | joins when its check passes |
| Clef Flash | 24 GB | joins when its check passes |
| AutoTrust Decider Pro 35B | 96 GB | joins when its check passes |
| mammoth-1 | 96 GB | EU operators only |

REAX 1.4 replaces REAX 1.3 when it is released. "Planned at start" is a plan, not a promise: a model opens only after its
reference check passes.

## How to start
```sh
curl -fsSL https://raw.githubusercontent.com/reaxlabs/reax-miner-kit/v0.2.0/install.sh \
  | bash -s -- --model <model id> --commit <commit announced at https://reax.dev/mine/>
./reaxctl models                     # every model, what it needs, whether this machine fits
./reaxctl switch --model <model id>  # move to another model; rolls back if the new model fails its check
```
Until the start, `reaxctl models` lists today's pools; the pot models appear there with the release that starts the pots. Today's pools keep working unchanged.

The draft [portfolio descriptor](../releases/portfolio-v1.json) contains the model IDs and pinned weight revisions. Runtime digests, manifests, weight sizes and disk requirements are placeholders; serving constraints in its notes are informational and are not enforced by this draft. There is no local descriptor option for `reaxctl models`; once a compatible release publishes the portfolio in `current.json`, use `./reaxctl models --status coming` to list the models awaiting activation. Until then the default command lists the current release pools.
