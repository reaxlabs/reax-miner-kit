# Inference mining v2

**Rules version 1.1, 25 November 2026.** Same rules as version 1.0, rewritten so they read clearly at first sight, with
worked examples and answers to the questions miners have asked. No rule changed. The binding wording is in
[The rules in full](#the-rules-in-full-binding-text); the sections before it explain it. These rules describe the planned
validator release; its start is announced at least 24 hours ahead.

## What you mine

You serve one of the decision models on our list (see [Model list](#model-list-v1-pots)) on your own GPU or CPU, with the
exact pinned weights and runtime from the miner kit. Validators send your node secret test questions and load probes, and
later real customer requests. You are paid for verified serving capacity in that model.

- **Every listed model has its own pot.** Pick a model your hardware fits; you earn from that model's pot.
- **Today's four pools (s1-fast, s1-lite, s1-cpu, global s1-pro) end** 7 days after the start. Switching to a pot model is
  one command.
- **Anyone can join a global pot.** No seat, application or permission is needed. Register a hotkey and serve.
- **EU operators** are a separate, contracted layer for customers whose data must stay in the EU (see Global pots and the EU layer).

## How you are paid

1. **45 % of all miner emissions** of subnet 92 go to inference mining.
2. That 45 % is split into **one pot per listed model**. Bigger models get bigger pots, by the GPU class the model needs:

   | GPU class | Weight |
   |---|---|
   | 96 GB | 1.0 |
   | 48 GB | 0.6 |
   | 24 GB | 0.35 |
   | 16 GB | 0.15 |
   | CPU | 0.03 |

   A pot's budget = 45 % x its weight / the sum of the weights of all listed pots.
3. **Inside a pot you are paid by your share of the verified capacity** in that model: your capacity / all operators'
   capacity in the pot. Capacity is measured as today: secret reference answers, surprise load probes that hit all hotkeys
   of the pot at the same moment, and a strike resets you to zero. In global pots an answer must match the pinned reference
   exactly.
4. **Your pay = pot budget x your share, but never more than the cap** (see Pots and caps).

Money a pot cannot pay out (no operator yet, or above someone's cap) is **not** handed to other miners or other pots.
During the first 7 days it goes to today's pools; after that it is burned. Rewards are variable subnet emissions and can
be zero. Nothing already earned is taken away.

## Pots and caps

- **Operator** = everything that shares a coldkey, a /24 network or a multi-node gateway. Several hotkeys or coldkeys on
  one /24 are one operator.
- **One operator gets at most 50 % of a pot.** This is why every model pays a second and a third operator: we want
  redundancy, at least two independent operators per model.
- **Only a full GPU unit earns the full 50 %.** A full unit = one GPU of the model's class running at reference speed in our
  probes. With less, the cap shrinks in proportion: 0.4 units can earn at most 20 % of the pot.
- **The cap is per pot.** One operator can hold up to 50 % in each pot it serves.
- **Pioneer bonus.** On a newly listed model (or one that had no operator for 7 days), the 1st, 2nd and 3rd operator that
  qualifies with at least one full unit gets +50 %, +25 % and +12.5 % on its pay. Each bonus falls in a straight line to 0
  within 48 hours of that operator's own qualification. It is paid on top of the cap, from the pot's unpaid budget. Once per
  coldkey per model; at most one bonus round per model every 30 days.

## Worked examples (in alpha, not prices)

All examples count **alpha per 1,000 alpha of miner emissions**: whatever the subnet emits to miners, 450 of every 1,000
go to the inference pots. They assume all 13 global pots in the model list are listed (weights add up to 8.63: seven
96 GB models, one 48 GB, two 24 GB, two 16 GB, one CPU) and no EU-layer pay. EU-layer pay comes out of the same 45 % and
makes every pot slightly smaller.

| Pot | Budget per 1,000 alpha of miner emissions |
|---|---|
| a 96 GB model (e.g. d3) | 450 x 1.0 / 8.63 = **52.1** |
| the 48 GB model (Blink) | 450 x 0.6 / 8.63 = **31.3** |
| a 24 GB model (e.g. elephant-1) | 450 x 0.35 / 8.63 = **18.3** |
| a 16 GB model (e.g. Kev 4B) | 450 x 0.15 / 8.63 = **7.8** |
| the CPU model (Laya) | 450 x 0.03 / 8.63 = **1.6** |

**Example A, two operators and the cap.** In the d3 pot (52.1), operator A has 3 full units and operator B has 1.
A's share is 75 %, capped at 50 %: **26.1**. B's share is 25 %: **13.0**. The remaining 13.0 is not paid to A or B; in the
first 7 days it goes to today's pools, then it is burned. If a third operator C joins with 2 units, the shares become A 50 %,
B 16.7 %, C 33.3 %: A **26.1**, B **8.7**, C **17.4**, and nothing is left unpaid.

**Example B, a small node alone.** Operator D is alone in the elephant-1 pot (18.3) with a slow GPU worth 0.4 units. Its
share is 100 %, but its cap is 0.4 x 50 % = 20 %: **3.7**. With one full unit it would get the full 50 %: **9.1**.

**Example C, pioneer bonus.** Operator E is the first to qualify in a newly listed 96 GB pot (52.1), with one full unit, and
is alone. Base pay: 50 % = **26.1**. Bonus +50 % at qualification: **39.1**; after 24 hours +25 %: **32.6**; after 48 hours
no bonus: **26.1**. The bonus comes from the half of the pot that E cannot earn under the cap.

**Example D, TEE bonus (once it is live).** Three operators with 2 full units each serve the Karpo pot (52.1). F runs a
verified TEE node, so its 2 units count as 3. Shares: F 3/7 = 42.9 %, G and H 2/7 = 28.6 % each: F **22.3**, G and H
**14.9** each. Without the TEE each would get a third: **17.4**.

## TEE: what, why, how to join

- **What.** A TEE node is a confidential VM with a confidential-computing GPU. The CPU (AMD SEV-SNP or Intel TDX) and the
  GPU (NVIDIA, confidential computing on) prove by attestation which software runs and that nobody, including the host,
  can read the requests. Our verifier checks this about every hour; a verdict is valid for 6 hours.
- **Why.** Customers send private business decisions. With a TEE we can show them that a miner cannot read their data.
  That is what lets us send more real customer traffic to miners.
- **What it earns.** GPU-hours with a valid TEE verdict count **1.5x** toward your pot share, from the moment validators
  send scored pot traffic to the attested node itself. We confirm that start in #92; there is no date until then.
- **When it becomes required.** On **Monday 30 November** we check every listed model's open global pot. If every one has
  at least **two TEE operators** (a valid verdict and at least one full GPU unit in that pot), TEE is required in every pot
  from **Thursday 3 December 2026**. If even one model falls short, TEE stays a bonus everywhere and we name the missing
  models and the next check date in #92 on 30 November.
- **Hardware.** RTX PRO 6000 Blackwell Server Edition, H100, H200 or B200 with confidential computing on. Proven end to end:
  AMD SEV-SNP (EPYC Turin) with RTX PRO 6000 on a Verda confidential VM.
- **How to join (about 15 minutes for the T1 level).** Rent a confidential VM, install the node stack and the attestation
  agent from the kit's `tee/` folder with your hotkey, then say in #92 that your node is ready; we tell you how to register
  it privately. There is no onboarding limit; nodes are registered in the order they arrive. Step by step:
  [TEE-OPERATOR.md](TEE-OPERATOR.md).

## Global pots and the EU layer

| | Global pots | EU layer |
|---|---|---|
| Who | anyone, permissionless | operators under contract (operator agreement) |
| Where | anywhere; a global TEE node needs **no** EU site | EU sites only; data stays in the EU |
| TEE | bonus now; required from 3 Dec if the 30 Nov check passes | required |
| Answer check | answers must match the pinned reference exactly | our answer test (answer quality instead of bit-identical answers), so as many parallel requests as the hardware answers correctly |
| Pay | share of the model's pot (see How you are paid) | per verified, attested GPU-hour; always more than the same GPU class earns in the global pots; rate in the agreement |
| Bonuses | pioneer bonus, TEE 1.5x | none (the rate already includes them) |

A GPU, host, IP or hotkey cannot be in both layers. EU interest: ask in #92 and we route you to the operator agreement.

## Timeline and checks

| When | What happens |
|---|---|
| Now | Today's four pools run unchanged. The v2 kit is out. |
| Start (announced at least 24 h ahead, at the latest Tuesday 1 December 2026) | Pots open for every model whose reference check has passed; the others join when their check passes, without further notice. |
| First 7 days after the start | Today's pools are paid only what the pots do not pay out, never more than today. Then they end. |
| Once validators send scored pot traffic to attested nodes (confirmed in #92) | TEE units count 1.5x. |
| Monday 30 November 2026 | TEE check: at least two TEE operators in every listed model's open global pot? All or nothing. |
| Thursday 3 December 2026 | TEE required in every pot, only if the 30 November check passed. |
| Any time | New models can be added without notice. Removing a model is announced at least 24 hours ahead, and its pot pays during that time. |

## Questions miners asked

- **Do I need a seat or approval for a global pot?** No. Global pots are permissionless. Only the EU layer needs an
  agreement.
- **Can I test a pot model before the start?** The kit is out now, but pot models appear in `reaxctl models` (and in
  `reaxctl selfcheck`) only with the release that starts the pots. Until then the kit serves today's pools.
- **Can one hotkey serve several pot models?** One model per hotkey per machine is the supported path; the starting release
  confirms whether one hotkey can hold several pot models.
- **I run two machines with different coldkeys on different networks. One operator or two?** Two, if they share no coldkey,
  no /24 network and no multi-node gateway. Both are probed at the same moment, so if they share one backend each shows only
  part of the capacity.
- **Several coldkeys on one /24?** One operator: one 50 % cap and one pioneer place per model. The cap is per pot, so serving
  different models from one /24 is fine.
- **Can one GPU serve several pots?** Allowed, but no gain. Each pot measures capacity in its own model, so a split GPU shows
  only part of its capacity in each, and the full cap needs one full unit per pot.
- **Do the pot budgets count only listed pots? Does an empty pot raise the others?** Budgets are divided by the weights of
  all listed pots. A pot that has not opened or has no qualified operator does not raise the others.
- **Is the 50 % cap applied before or after the shares are computed?** After: pay = pot budget x min(your share, your cap).
  What the cap cuts is not passed on to other operators.
- **Does an early TEE verification start my pioneer bonus or reserve a place?** No, they are separate. The pioneer clock
  starts when you qualify in a model's pot with a full unit. A TEE verdict only means your node counts as TEE once the TEE
  bonus is live. A node can earn both.
- **Is one RTX PRO 6000 Server Edition a full unit?** It is a 96 GB-class GPU. Whether it is a full unit is measured: if it
  reaches reference speed in our probes, yes; if slower, its cap shrinks in proportion.
- **Does a global TEE node have to be in the EU?** No. Only the EU layer requires EU sites.
- **How exactly does the 30 November check work?** All or nothing over the open global pots: every listed model needs at
  least two TEE operators, each with a valid verdict and one full GPU unit in that pot. Otherwise TEE stays a bonus.
- **Is there a limit on TEE onboarding?** No fixed limit; nodes are registered in the order they arrive.
- **Where is the TEE scored?** Inside the confidential VM, through the endpoint your hotkey commits on chain. WireGuard to
  our gateway is only needed for EU-layer serving.

## The rules in full (binding text)
1. **One pot per listed model.** The inference part of miner emissions rises to **45 %**. It is split into one pot per listed model, weighted by the GPU class the model needs
   (96 GB 1.0, 48 GB 0.6, 24 GB 0.35, 16 GB 0.15, CPU 0.03). Later the weights may also follow customer demand per model;
   any such change is announced before it applies.
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
    scored pot traffic to the attested node itself; we confirm that start in #92. Global TEE nodes have no EU location
    requirement; only the EU layer (rule 8) requires EU sites. From **Thursday 3 December 2026**, TEE is required in
    every pot if, on **Monday 30 November**, every listed model has at least two TEE operators in its open global pot.
    A TEE operator counts for a pot when it has a valid verdict and at least one full GPU unit in that pot. The check is
    all-or-nothing: if even one model falls short, TEE stays a bonus everywhere, and we name the missing models and the
    next check date in #92 on 30 November. There is no fixed onboarding limit; TEE nodes are registered in the order
    they arrive. TEE-capable GPUs: RTX PRO 6000 Server Edition, H100, H200 or B200 with confidential computing on. Setup
    notes for TEE nodes: [TEE-OPERATOR.md](TEE-OPERATOR.md). The EU layer already requires a TEE (rule 8).



## Model list v1 (pots)
| Model | GPU class | At start? |
|---|---|---|
| d3 (vllm-sr/d3) | 96 GB | planned at start |
| Karpo Decision 31B | 96 GB | planned at start |
| Clef 27B (Cloudflare/clef) | 96 GB | planned at start |
| Blink v0.3 NVFP4 | 48 GB, Blackwell only | planned at start |
| elephant-1 (bf16) | 24 GB | planned at start |
| Torchcast Decision 4B | 16 GB | planned at start |
| Kev 4B (no batching, one at a time) | 16 GB | planned at start |
| Laya 421M | CPU (or GPU in fp32) | planned at start |
| Torchcast Decision 31B (no batching, one at a time) | 96 GB | joins when its check passes |
| Perplexity Decider v1.3 | 96 GB | joins when its check passes |
| Clef Flash | 24 GB | joins when its check passes |
| AutoTrust Decider Pro 35B | 96 GB | joins when its check passes |
| mammoth-1 | 96 GB | EU operators only |

REAX 1.3 is not part of the v2 pots for now. "Planned at start" is a plan, not a promise: a model opens only after its
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
