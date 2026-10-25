"""Safe tests for the autonomous moderation decisions — no Discord, no side effects."""

from datetime import datetime, timedelta, timezone

from chaosx_bot import moderation_guard as guard

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def msg(mid, channel, author=7, content="hello", minutes=0, **kwargs):
    return guard.Message(
        id=mid,
        channel_id=channel,
        author_id=author,
        content=content,
        created_at=NOW + timedelta(minutes=minutes),
        **kwargs,
    )


def test_same_link_across_channels_is_a_raid():
    messages = [msg(1, 10, content="join https://evil.example/x"), msg(2, 11, content="join https://evil.example/x"), msg(3, 12, content="join https://evil.example/x")]
    verdict = guard.analyse(messages)
    assert verdict.action == "ban"
    assert verdict.rule == "3.4"
    assert len(verdict.delete_message_ids) == 3


def test_one_link_is_not_a_raid():
    assert guard.analyse([msg(1, 10, content="look at https://github.com/klimPaskov/Chaos-Redux")]).action == "none"


def test_repeated_identical_messages_across_channels_ban():
    messages = [msg(i, 20 + i, content="FREE SKINS CLAIM NOW") for i in range(3)]
    assert guard.analyse(messages).rule == "3.4"


def test_free_nitro_is_a_scam_link():
    verdict = guard.analyse([msg(1, 10, content="free nitro gift here https://x.example")])
    assert verdict.action == "ban" and verdict.rule == "4.3"


def test_slur_escalates_to_ban_with_the_right_rule():
    verdict = guard.analyse([msg(1, 10, content="you are a retard honestly")])
    assert verdict.action == "ban" and verdict.rule == "2.2"


def test_a_threat_is_a_ban():
    assert guard.analyse([msg(1, 10, content="i am going to kill you")]).rule == "2.2"


def test_phone_or_email_is_flagged_but_not_punished_blind():
    verdict = guard.analyse([msg(1, 10, content="call me on +1 555 123 4567 tonight")])
    assert verdict.action == "delete" and verdict.rule == "5.1" and verdict.unsure


def test_plain_insult_warns_and_admits_it_might_be_banter():
    verdict = guard.analyse([msg(1, 10, content="you idiot")])
    assert verdict.action == "warn" and verdict.rule == "2.1"
    assert verdict.unsure is True


def test_sexual_attachment_bans():
    verdict = guard.analyse([msg(1, 10, content="look", has_sexual_or_gore=True)])
    assert verdict.action == "ban" and verdict.rule == "4.1"


def test_staff_and_owner_are_never_touched():
    assert guard.analyse([msg(1, 10, content="you are a retard")], staff=True).action == "none"


def test_ladder_follows_section_7():
    assert guard.next_step(0) == ("warn", "7.1")
    assert guard.next_step(1) == ("timeout", "7.2")
    assert guard.next_step(2) == ("timeout_and_kick", "7.3")
    assert guard.next_step(3) == ("ban", "7.4")
    assert guard.next_step(4) == ("ban", "7.4")


def test_explanation_cites_the_rule_in_both_places():
    verdict = guard.Verdict(action="delete", rule="3.5", reason="unsolicited invite", confidence=0.8)
    dm, channel = guard.explain(verdict)
    assert "§ 3.5" in dm and "§ 3.5" in channel
    assert "delete" in dm.lower()


def test_established_member_bans_need_owner_approval():
    verdict = guard.analyse([msg(1, 10, content="free nitro gift here")], member_is_established=True)
    assert verdict.needs_owner_approval is True
    request = guard.approval_request(verdict, author="member", author_id=42, sample="free nitro gift here")
    assert "§ 4.3" in request and "<@42>" in request and "✅" in request


def test_unsure_cases_ask_a_human_instead_of_acting():
    verdict = guard.analyse([msg(1, 10, content="call me on +1 555 123 4567 tonight")])
    text = guard.unsure_request(verdict, author="member", author_id=42, sample="call me...")
    assert "Unsure" in text and "no action taken" in text
