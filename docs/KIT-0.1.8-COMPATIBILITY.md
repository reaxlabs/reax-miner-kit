# Kit 0.1.8 compatibility with the mammoth descriptor

Upgrade to kit 0.1.9 before installing or updating s1-pro. Older controllers cannot mount the pinned FLA configs.
The new descriptor leaves `enabled_at_launch=false` for s1-pro; kit 0.1.9 understands its `runtime.enabled` contract.

| Old kit command | Observed behavior with the new descriptor |
| --- | --- |
| Descriptor refresh | Accepts and caches the new descriptor; one-hour cache remains |
| Fresh s1-pro install | Exit 12, "This pool is not enabled." No compose rendered |
| Update check | Reports a release and manifest change; no running files changed |
| Update without acceptance | Exit 11, "Approve the manifest change." No manager calls |
| Update with acceptance | Exit 12, "This pool is not enabled." Rollback retains old files; existing rollback path calls compose up -d |
| Doctor on a fitting GPU | Sees 48 GB/public weights but can still report real_mining_possible=true; does not understand the disabled pool contract |
| Selfcheck on existing Rune | Uses the installed old miner and sidecar; may pass Rune answers although validators refuse Rune |
| Running without update | Descriptor refresh does not stop or rewrite existing containers; validators continue refusing Rune |

The fixed old refusal message cannot say "upgrade the kit" through descriptor data alone. The old doctor and selfcheck
paths also lack a per-pool capability gate. Invalidating the descriptor makes old kits fall back to their bundled Rune
pins, and disabling global live/source/image flags would also break the other pools. These old-controller limitations
remain release-review concerns; the descriptor is not proof that an existing Rune installation is current.

For s1-fast, s1-lite and s1-cpu, descriptor refresh changes no running image. Explicit update apply replaces the shared
miner image under the existing update policy. Their runtime digests, weights, manifests and per-model probe limits stay
at their previous values. The new miner contains those unchanged manifests and defaults; no GPU run of those stacks is
claimed by this kit release.
