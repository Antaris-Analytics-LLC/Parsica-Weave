#!/usr/bin/env python3
"""The shipped documents address a person. REVIEW-2026-10.md, follow-up, class (c).

At e480bbd ONBOARDING.md 3-4 said "This document is for the agent, first and foremost. If you
are an agent and a session-start injection brought you here, this is your thread: pick it up and
follow it", pointed the agent at TO_THE_NEXT_AGENT.md (a letter from one agent to the next,
signed with a persona), and closed at 236-239 with "If you are an agent reading this on your
first boot: ... Work like it. Write your lessons. Query before you guess. And come back." A
document that instructs future agents is an instruction channel. ONBOARDING.md now addresses the
person who installed Weave, and TO_THE_NEXT_AGENT.md is out of the tree (history keeps it).

  1. TO_THE_NEXT_AGENT.md does not exist and no shipped document points at it.
  2. ONBOARDING.md opens for the person who installed Weave, and no shipped document carries the
     phrases that made the old text an agent-to-agent channel.

The audit record under docs/ quotes the old passages as findings and is not a shipped document,
so it is not scanned here; the plain-ASCII gate covers it.

Runs under pytest and standalone (python test_docs_address_a_person.py).
"""
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_DOCS = ["README.md", "ONBOARDING.md", "AUTONOMOUS_ENRICHMENT.md", "SECURITY.md",
                os.path.join("skills", "weave", "SKILL.md")]
CHANNEL_PHRASES = ("this document is for the agent", "if you are an agent", "pick it up and follow it",
                   "to the next agent", "to_the_next_agent", "from me to you",
                   "the first agent onboarded", "signed,")


def _shipped():
    paths = [os.path.join(ROOT, d) for d in SHIPPED_DOCS]
    paths += sorted(glob.glob(os.path.join(ROOT, "commands", "*.md")))
    return [p for p in paths if os.path.isfile(p)]


def test_the_letter_to_the_next_agent_is_out_of_the_tree():
    assert not os.path.exists(os.path.join(ROOT, "TO_THE_NEXT_AGENT.md")), \
        "TO_THE_NEXT_AGENT.md is back in the tree; a document that instructs future agents is an instruction channel"


def test_onboarding_opens_for_the_person_who_installed_weave():
    head = open(os.path.join(ROOT, "ONBOARDING.md"), encoding="utf-8").read(1500).lower()
    assert "for the person who installed weave" in head
    assert "for the agent, first and foremost" not in head


def test_no_shipped_document_carries_an_agent_to_agent_channel_phrase():
    checked = 0
    for p in _shipped():
        checked += 1
        low = open(p, encoding="utf-8", errors="replace").read().lower()
        for phrase in CHANNEL_PHRASES:
            assert phrase not in low, "%s still carries %r" % (os.path.relpath(p, ROOT), phrase)
    assert checked >= 7, "discovery broke: too few documents checked (%d)" % checked


def _run():
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print("  ok  %s" % name)
    print("[test_docs_address_a_person] %d/%d passed" % (len(fns), len(fns)))


if __name__ == "__main__":
    _run()
