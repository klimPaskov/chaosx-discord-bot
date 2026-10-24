"""Official links must be exact, complete, and never invented."""

from chaosx_bot.resources import OFFICIAL_RESOURCES, help_resources_section, resources_block


def test_channel_and_donation_are_present_and_exact():
    urls = {r.key: r.url for r in OFFICIAL_RESOURCES}
    assert urls["youtube"] == "https://www.youtube.com/@ChaosRedux"
    assert urls["donate"] == "https://ko-fi.com/klimpaskov"


def test_every_resource_has_label_url_and_when():
    for resource in OFFICIAL_RESOURCES:
        assert resource.key and resource.label
        assert resource.url.startswith("https://")
        assert resource.when


def test_prompt_block_lists_every_url_and_forbids_invention():
    block = resources_block()
    for resource in OFFICIAL_RESOURCES:
        assert resource.url in block
    assert "only URLs you may give" in block


def test_help_section_contains_youtube_and_donation():
    section = help_resources_section()
    assert "https://www.youtube.com/@ChaosRedux" in section
    assert "https://ko-fi.com/klimpaskov" in section


def test_no_private_repo_leaks_into_member_facing_text():
    text = resources_block() + help_resources_section()
    assert "chaos-redux-vault" not in text
    assert "chaosx-discord-bot" not in text


def test_community_help_lists_official_links():
    from chaosx_bot.bot import community_help_text

    help_text = community_help_text()
    assert "https://www.youtube.com/@ChaosRedux" in help_text
    assert "https://ko-fi.com/klimpaskov" in help_text
