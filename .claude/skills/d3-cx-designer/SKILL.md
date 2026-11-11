---
name: d3-cx-designer
description: Produce the D3 CX design for Asisten Kuota from the running prototype — happy path, 3 edge cases, sample conversations, handoff rules — and export it to PDF. Use for any D3 work.
---

# D3 CX Designer — Asisten Kuota

## Source of truth = the code and real runs
- Run the scenarios first (P1 happy path; P3 insufficient balance; P4 outage + angry customer; P2 and P5 supporting). Capture transcripts and trace summaries via `GET /api/trace/<session_id>`.
- Sample conversations are taken from real runs, lightly cleaned, still masked. Never invent agent behaviour the code does not have.
- Anything the CX needs but the code lacks goes to the **Gap list**, linked to a DEF-ID or a new change request for the D4 skill.

## Required content of `docs/D3_CX_Design.md`
1. CX principles and tone of voice: Bahasa Indonesia, empathetic, concise, transparent about price and fee, no pressure selling.
2. Happy path journey table: step · customer emotion · agent action · UI element · tool call.
3. Edge cases, each with journey table + real sample conversation:
   (a) insufficient balance (Paket Darurat), (b) angry customer, (c) genuine network outage.
4. Human handoff rules: trigger · detection logic as implemented (cite `core/guardrails.py`) · what the human receives (masked summary + ref ID) · customer message · SLA (assumption).
5. Consent and privacy moments.
6. Failure messaging: timeout, PENDING_VERIFICATION, service unavailable, AI rate-limited.
7. KPI link: conversion +15%, call-center −20%, CSAT ≥ 4.2.
8. Traceability table: D3 rule → code location → test.
9. Gap list.

## Output
- Markdown first, then STOP for the owner's review, then export `docs/D3_CX_Design.pdf` with clean headings and tables.
