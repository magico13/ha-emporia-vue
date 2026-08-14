"""Helpers for interpreting Emporia channel metadata."""

from collections.abc import Iterable
from typing import Protocol


class ChannelMetadata(Protocol):
    """The channel fields used to classify merged Emporia circuits."""

    channel_num: str
    parent_channel_num: str | None
    type: str


def merged_channel_is_bidirectional(
    channel: ChannelMetadata, channels: Iterable[ChannelMetadata]
) -> bool:
    """Return whether a merged channel has one or more all-bidirectional children."""
    if channel.type.lower() != "merged":
        return False

    children = [
        candidate
        for candidate in channels
        if candidate.parent_channel_num == channel.channel_num
    ]
    return bool(children) and all(
        "bidirectional" in child.type.lower() for child in children
    )
