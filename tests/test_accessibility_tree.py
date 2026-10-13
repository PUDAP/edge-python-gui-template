"""Offline regression tests for reusable accessibility snapshot traversal."""

import unittest

from gui_driver import GuiDriver


class AccessibilityTreeTests(unittest.TestCase):
    def test_walk_preserves_sibling_order_and_excludes_unrelated_nodes(self):
        elements = [
            {"element_index": 4, "parent_index": 2},
            {"element_index": 2, "parent_index": 1},
            {"element_index": 3, "parent_index": 1},
            {"element_index": 1},
            {"element_index": 5, "parent_index": 99},
        ]
        children = GuiDriver._children_by_parent(elements)
        self.assertEqual(children[None], [elements[3]])
        descendants = GuiDriver._descendants(children, 1, "Malformed tree")
        self.assertEqual([element["element_index"] for element in descendants], [2, 3, 4])

    def test_leaf_has_no_descendants(self):
        self.assertEqual(GuiDriver._descendants({}, 1, "Malformed tree"), [])

    def test_rejects_cycles_and_duplicate_indices(self):
        snapshots = [
            [{"element_index": 1, "parent_index": 1}],
            [{"element_index": 2, "parent_index": 1},
             {"element_index": 1, "parent_index": 2}],
            [{"element_index": 2, "parent_index": 1},
             {"element_index": 2, "parent_index": 1}],
        ]
        for elements in snapshots:
            with self.subTest(elements=elements):
                children = GuiDriver._children_by_parent(elements)
                with self.assertRaisesRegex(RuntimeError, "^Malformed tree$"):
                    GuiDriver._descendants(children, 1, "Malformed tree")
