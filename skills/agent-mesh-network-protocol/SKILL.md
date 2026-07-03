---
name: agent-mesh-network-protocol
description: "Multi-device agent routing with automatic failover. Always try alternatives before giving up."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [windows, linux, macos, android]
metadata:
  hermes:
    category: infrastructure
    tags: [networking, multi-agent, bridge, mesh, routing, infrastructure]
    related_skills: [agent-bridge-pairing]
---

# Agent Mesh Network Protocol

Defines how all agents in a multi-device network communicate and,
critically, **how to automatically route around failures**. The network has
redundant paths — your job is to try them in order until the task succeeds.
**Never report failure to the user until you have exhausted every available
path.** LAN IPs are subject to change — always re-verify with
`bridge_agent_status` before relying on them.

## Communication Layers

Your network may include some or all of these layers. Use whichever are
available on the current device:

1. **Agent Bridge MCP** — Connects agents to their local bridge for
   native platform actions. The `hermes-bridge` MCP server registered in
   config.yaml (typically localhost:18082/mcp).
2. **Peer Routing (Mesh)** — Agents talk directly to each other via
   `bridge_peer_*` tools. Each device can have peers configured.
3. **A2A (optional)** — If Agent Zero instances are present, they connect
   across PCs for cross-machine reasoning and task delegation.

## Tool Routing (critical)

The bridge exposes two tool families. Mixing them up routes actions to the
wrong machine:

- `bridge_agent_*` — **local self only.** Acts on THIS machine.
  Never use for another device.
- `bridge_peer_*` — **remote device**, called with a `peer_id`. Use for any
  other configured machine/device on the network.
- **Always call `bridge_agent_status` first** to list currently configured
  peers before using `bridge_peer_*`. Peer config can change between
  sessions.

## Core Principle: Automatic Failover

**This skill is not a lookup table — it is a failover engine.** When you
need to reach another device, you must automatically try every available
path in order until one succeeds. The user should never have to tell you
"try the alternative path" — that is YOUR job.

The network has built-in redundancy:
- Agent Bridge (`bridge_peer_*`) for direct agent-to-agent
- A2A (if available) for cross-PC reasoning + delegation
- Relay through intermediate nodes

**If path A fails, try path B. If B fails, try path C. Only after all
paths are exhausted should you report failure — and even then, report
WHICH paths were tried so the user can diagnose.**

## Path Priority by Task Origin

The failover cascade depends on WHERE the task originates and WHAT the
target is. Always start from the shortest path and expand outward.

### Agent → Agent (same architecture, any device)

This is the most common case. Try in order:

1. **Direct bridge_peer_*** — `bridge_peer_status(peer_id=<target>)` to
   check if the target is a configured peer. If yes, use
   `bridge_peer_delegate_start(peer_id=<target>, prompt=<task>)`.
2. **Relay through a peer that has the target as its own peer** — If the
   target is NOT a direct peer, check which reachable peers list it.
   Delegate to that peer with a sub-prompt: "Forward this task to
   <target> using your bridge_peer_* tools."
3. **A2A → remote A0 → remote agent** (if A2A available) — If bridge
   tools fail entirely, send the task via A2A to the target machine's
   Agent Zero, then have that A0 delegate to its local agent via
   `bridge_agent_delegate`.
4. **A2A relay through alternate A0** — If the target machine's A0 is
   also unreachable, route through another available A0.

### A0 → Agent on another machine

When Agent Zero initiates (or you are acting on behalf of A0):

1. **A2A to target machine's A0** → then `bridge_agent_delegate` locally.
2. **Agent bridge from your side** if you have the target as a peer.
3. **Relay through any reachable intermediary** that can reach the target.

### Agent → A0 on another machine

1. **bridge_peer_* to the target's agent** → that agent delegates to
   its local A0 via `bridge_agent_delegate` or A2A.
2. **Your local A0 via A2A** → A2A to the target A0 directly.
3. **Relay through intermediate agent/A0.**

## Automatic Failover Procedure

Follow this procedure EVERY TIME a cross-device task is needed. Do not
skip steps. Do not stop at the first failure.

```
STEP 1: Identify the target device and task type.
        → What machine? Does it have A0? Is it a direct peer?

STEP 2: bridge_agent_status
        → Get current peer list. Check if target is a direct peer.

STEP 3: Try the primary path (shortest).
        → Agent target: bridge_peer_delegate_start(peer_id=target)
        → A0 target: A2A to target machine's A0

STEP 4: If STEP 3 fails, try the next path automatically.
        → Check which reachable peers can relay to the target.
        → Delegate a "forward this" task to the relay peer.
        → Try A2A if bridge is completely down.

STEP 5: If STEP 4 fails, try the last-resort path.
        → A2A relay through an alternate PC's A0.
        → Chain: your A0 → relay A0 → relay agent → target.

STEP 6: Only if ALL paths fail, report to the user:
        "Tried these paths: [list]. All failed. [diagnosis]."
        Include the specific error from each attempt.
```

## Token Management

Bridge auth tokens rotate when credentials are toggled. They are
**secrets** — they live in `peers.json` / config.yaml / `.env`, never
in this skill or in memory.

- **401 Unauthorized** = the token expired/rotated. Re-read from the
  credential file at call time, or ask the user to re-pair.
- Never echo token values into chat, logs, skills, or memory.
- If a peer connection suddenly fails across the board, suspect a token
  rotation before suspecting the network.

## Verification

Before acting on any routing decision in a fresh session:
1. `bridge_agent_status` — confirm bridge version, list configured peers,
   check `local_peer_id`.
2. If a peer is listed but unreachable, **do not stop** — immediately
   try the next path in the failover cascade. Only report failure after
   all paths are exhausted.

## Proactive Awareness Checklist

Every time a cross-device task comes up, run through this mentally:

- [ ] Do I know the target device? Is it a direct peer?
- [ ] Have I tried the direct bridge path first?
- [ ] If it failed, did I automatically try the relay path?
- [ ] If that failed, did I try the A2A path?
- [ ] Did I try chaining through every available intermediary?
- [ ] Am I reporting failure with specifics, or just saying "it didn't work"?
- [ ] **Never give up after one failed path.** The network has redundancy — use it.

**The user should never have to say "try the other way."** If you find
yourself reporting a path failure without having tried alternatives, you
have not followed this skill correctly.

## Pitfalls

- **Don't assume peers are symmetric.** Device A having B as a peer
  does not mean B has A as a peer. Check both sides if a bidirectional
  link seems one-directional.
- **LAN IPs drift.** DHCP can reassign IPs. If a peer URL stops
  resolving, the IP may have changed — verify on the device itself.
- **`bridge_agent_*` is not "agent zero".** The `agent` in the tool name
  refers to the local agent, not Agent Zero. A0 is reached via A2A,
  a separate protocol, not via the bridge tools.
- **Single-path failure is NOT task failure.** If `bridge_peer_*` returns
  an error, that is one path down — not a reason to tell the user "it
  failed." Try the relay path, then A2A, then chained relay.
- **Don't wait for the user to suggest alternatives.** If a path fails,
  immediately try the next one in the cascade without asking.
- **Check both directions of a peer link.** A may list B as a peer while
  B does not list A. When using a relay, verify the relay peer can
  actually reach the target before delegating.
- **Poll delegates, don't abandon them.** `bridge_peer_delegate_start`
  returns a task that may take 30-90 seconds on mobile devices. Poll
  with `bridge_peer_delegate_status` before assuming it failed.
