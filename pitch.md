# AI Wallet Guard MVP v3 — Pitch Notes

## One-liner

AI Wallet Guard is a financial firewall for autonomous AI agents.

## Problem

As AI agents start buying data, renting compute, using paid APIs, and transacting with other agents, companies need a way to control agent spending before money moves.

A bad prompt, coding bug, retry loop, or prompt injection can create thousands of microtransactions before a human notices.

## Solution

AI Wallet Guard provides a real-time authorization layer between autonomous AI agents and paid services.

The agent can request spending, but the Guard decides whether the agent is allowed to spend.

## v3 MVP proof

v3 includes:

- Guard API
- AI Agent Simulator
- Dashboard
- Risk engine
- Spending limits
- Velocity detection
- Circuit breaker
- Alerts
- Audit logs

## Demo story

1. Normal agent requests $0.05 for data.
2. Guard approves.
3. Agent tries $2.00 above limit.
4. Guard blocks and freezes the agent.
5. Crawler gets stuck in payment loop.
6. Guard detects velocity and freezes it.
7. Dashboard shows alerts and audit trail.

## Positioning

Cloudflare WAF for AI spending.

Stripe Radar + Brex controls for autonomous AI agents.

## Why not build payment rails first?

Mastercard, Stripe, Coinbase, and others are building payment rails. AI Wallet Guard is the control layer above those rails.

It can integrate with cards, stablecoins, AP4M, x402, or internal API credits later.

## Initial customer

- AI startups
- SaaS companies using autonomous agents
- FinOps teams
- API-heavy data companies
- Enterprises testing agentic workflows

## Pricing idea

- Starter: $49/month
- Growth: $299/month
- Business: $1,000+/month
- Enterprise: by agent count, transaction volume, and compliance requirements