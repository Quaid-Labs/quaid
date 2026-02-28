"""Canonical default domain descriptions for memorydb."""

from __future__ import annotations

from typing import Dict

DEFAULT_DOMAIN_DESCRIPTIONS: Dict[str, str] = {
    "personal": "identity, preferences, relationships, life events",
    "technical": "code, infra, APIs, architecture",
    "project": "project status, tasks, files, milestones",
    "work": "job/team/process decisions not deeply technical",
    "health": "training, injuries, routines, wellness",
    "finance": "budgeting, purchases, salary, bills",
    "travel": "trips, moves, places, logistics",
    "schedule": "dates, appointments, deadlines",
    "research": "options considered, comparisons, tradeoff analysis",
    "household": "home, chores, food planning, shared logistics",
    "legal": "contracts, policy, and regulatory constraints",
}


def default_domain_descriptions() -> Dict[str, str]:
    return dict(DEFAULT_DOMAIN_DESCRIPTIONS)
