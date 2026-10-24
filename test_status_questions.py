"""Regression test: live data is fetched when the ask is about the present, and only then.

Both of Hoops' instructions are pinned here, because each fix for one broke the other:

* "Everything should be always dynamic... It must have access to all live data." — the bot must never answer a
  question about current state without live data, and must never invent "I can't pull current numbers here".
* "It should pull live data only when needed. This time is not wasted with each answer." — asked after a question
  about an unreadable video came back with a full server status report.

So `wants_live_facts` must be generous on live wording (missing a live question is the worse failure) and must
decline for questions that plainly are not about the present — knowledge, links, banter, small talk.

A missed *non*-live question is harmless now: RELEVANCE_RULE forbids reciting an unasked block, so a needless
fetch costs only a cached read.

    python3 test_status_questions.py
"""
import sys

sys.path.insert(0, "src")

from chaosx_bot.live_facts import wants_live_facts  # noqa: E402

# Must fetch: any way a member asks about the present.
LIVE_PHRASINGS = [
    # the exact wording that originally failed
    "Hey ChaosX what is the current status on things",
    # broad status asks
    "what's the current status",
    "status?",
    "how are things going",
    "how are we doing",
    "how's it going",
    "what's going on in the server",
    "what's new",
    "any news",
    "any updates",
    "give me an overview",
    "summary please",
    "quick recap",
    "what's the situation",
    "what's happening",
    "anything new?",
    "where do things stand",
    "what's the latest",
    # specific live asks
    "how many members do we have",
    "how many messages this week",
    "who's online right now",
    "who was the last to join",
    "what's the busiest channel this week",
    "any open issues",
    "what's marked needs testing",
    "who got warned recently",
    "what's the current version",
    "when was the last message in general",
    "is the repo quiet",
]

# Must NOT fetch: nothing about current state. A block here is wasted work, and (before RELEVANCE_RULE) it was
# what turned a video question into a status report.
NON_LIVE_PHRASINGS = [
    "how do I form Germany",
    "explain the zombie outbreak event chain",
    "where is the super event text defined",
    "what does the economy law do",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ what do you think",
    "hi",
    "thanks, appreciate it",
    "lol nice one",
    "good bot",
    "what's your favourite focus tree",
    "who made the mod",
]


def main() -> int:
    problems = []
    for text in LIVE_PHRASINGS:
        if not wants_live_facts(text):
            problems.append(f"no live data would be fetched for a live ask: {text!r}")
    for text in NON_LIVE_PHRASINGS:
        if wants_live_facts(text):
            problems.append(f"live data fetched for a question that is not about the present: {text!r}")

    for problem in problems:
        print("FAIL ", problem)
    print(
        f"checked {len(LIVE_PHRASINGS)} live phrasings (must fetch) "
        f"and {len(NON_LIVE_PHRASINGS)} non-live (must not)"
    )
    print("RESULT:", "all checks passed" if not problems else f"{len(problems)} failure(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
