from dataclasses import dataclass
import importlib.util
from pathlib import Path
import unittest


HELPERS_PATH = (
    Path(__file__).parents[1] / "custom_components" / "emporia_vue" / "helpers.py"
)
spec = importlib.util.spec_from_file_location("emporia_vue_helpers", HELPERS_PATH)
assert spec is not None and spec.loader is not None
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
merged_channel_is_bidirectional = helpers.merged_channel_is_bidirectional


@dataclass
class Channel:
    channel_num: str
    type: str
    parent_channel_num: str | None = None


class MergedChannelBidirectionalityTest(unittest.TestCase):
    def test_merged_channel_is_bidirectional_when_all_direct_children_are_bidirectional(self):
        merged = Channel(channel_num="97", type="Merged")
        children = [
            Channel(channel_num="12", type="FiftyAmpBidirectional", parent_channel_num="97"),
            Channel(channel_num="13", type="FiftyAmpBidirectional", parent_channel_num="97"),
        ]

        self.assertTrue(merged_channel_is_bidirectional(merged, [merged, *children]))

    def test_merged_channel_is_not_bidirectional_when_any_direct_child_is_not_bidirectional(self):
        merged = Channel(channel_num="97", type="Merged")
        children = [
            Channel(channel_num="12", type="FiftyAmpBidirectional", parent_channel_num="97"),
            Channel(channel_num="13", type="FiftyAmp", parent_channel_num="97"),
        ]

        self.assertFalse(merged_channel_is_bidirectional(merged, [merged, *children]))

    def test_merged_channel_is_not_bidirectional_when_it_has_no_direct_children(self):
        merged = Channel(channel_num="97", type="Merged")

        self.assertFalse(merged_channel_is_bidirectional(merged, [merged]))

    def test_non_merged_channel_is_not_classified_by_child_metadata(self):
        branch = Channel(channel_num="12", type="FiftyAmp")
        child = Channel(channel_num="13", type="FiftyAmpBidirectional", parent_channel_num="12")

        self.assertFalse(merged_channel_is_bidirectional(branch, [branch, child]))


if __name__ == "__main__":
    unittest.main()
