from chaosx_bot.bot import _chunk


def test_chunk_keeps_a_short_remainder_together() -> None:
    text = "a" * 1689 + "\n" + "b" * 276 + "\n" + "c" * 440

    parts = _chunk(text)

    assert len(parts) == 2
    assert all(len(part) <= 1900 for part in parts)
    assert parts[0] == "a" * 1689
    assert parts[1] == "b" * 276 + "\n" + "c" * 440