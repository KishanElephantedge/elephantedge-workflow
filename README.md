# Elephant Edge — Autonomous ABM System

Elephant Edge's own ABM strategy and autonomous outreach agent — finding and
engaging Elephant Edge's ideal customers, not a client's.

## Structure

- `ARCHITECTURE.md` — the reusable, company-agnostic 13-phase ABM framework
  this system is built against.
- `phase1-icp-notes.md` — raw ICP input, to be filled in from the ICP
  confirmation call (currently blocking).
- `phases/` — implementation of each phase, added as they're built.
- `data/` — working data (candidate lists, scored accounts, etc.). Not committed
  if it contains real prospect/contact data — check `.gitignore`.

## Status

Currently blocked on Phase 1 (ICP confirmation). See ARCHITECTURE.md status
checklist for full phase-by-phase progress.

## Relationship to other codebases

`synefi/` (separate project) is a reference for *pipeline structure/orchestration
in code* only — not a source of ICP, scoring, or messaging logic for this project.
