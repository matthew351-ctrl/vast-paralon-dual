"""v0.9.0 web page: one cell formatter, the Offer ends wording, short
coloured states, stacked phone cards, core vs Advanced settings, the tiered
change flow.
The page has no JS test runner, so these pin the page's own source."""

import re
import unittest

from vpdual.webui import PAGE


def js_function(name):
    """The source of `function name(...) {...}` in the page script."""
    start = PAGE.index("function " + name + "(")
    depth, i = 0, PAGE.index("{", start)
    while True:
        c = PAGE[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return PAGE[start:i]


class CellFormatter(unittest.TestCase):
    def test_one_formatter_for_nothing_and_booleans(self):
        src = js_function("cell")
        self.assertIn("v === null || v === undefined", src)
        self.assertIn("isNaN(v)", src)
        self.assertIn('v ? "yes" : "no"', src)
        self.assertIn('const NOTHING = "\\u2014";', PAGE)
        for word, shown in (("None", "NOTHING"), ("nan", "NOTHING"),
                            ("NaN", "NOTHING"), ("True", '"yes"'),
                            ("False", '"no"')):
            self.assertRegex(PAGE, r'"%s": %s' % (word, re.escape(shown)))

    def test_value_cells_go_through_it(self):
        self.assertIn("dd.textContent = cell(v)", js_function("dl"))
        self.assertIn("NOTHING", js_function("money"))
        self.assertIn("cell(", js_function("yesno"))
        self.assertIn("cell(v)", js_function("fmtValue"))
        self.assertIn('NOTHING + " (not set)"', js_function("fmtValue"))
        self.assertIn('cell(m.paralon.price)', PAGE)
        # no bare "-" placeholder left
        self.assertNotIn(': "-"', PAGE)
        self.assertNotIn('<b id="policy">-</b>', PAGE)


class OfferEnds(unittest.TestCase):
    def test_date_then_left_then_term(self):
        src = js_function("offerEnds")
        # "11-21 19:12 · 14 d left · rolling 7 d"
        self.assertIn('pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())', src)
        self.assertIn('" d left"', src)
        self.assertIn('" h left"', src)          # under a day: hours
        self.assertIn('"rolling " + ', src)
        self.assertIn('"fixed end"', src)
        self.assertIn('[" · " + left, hot], [" · " + term, "st-mut"]', src)
        # unlisted says so; no end date is "—", never a bare day count
        self.assertIn('[["unlisted", "st-mut"]]', src)
        self.assertIn('[[NOTHING, "st-mut"], [" · " + term, "st-mut"]]', src)
        self.assertNotIn(".toFixed(1)", src)

    def test_red_only_when_the_end_is_near(self):
        # fixed end: under 7 days; rolling: under 1 day (renewal late)
        self.assertIn('days < (dur ? 1 : 7) ? "st-bad" : ""',
                      js_function("offerEnds"))

    def test_shown_on_the_card_and_in_the_table(self):
        self.assertIn('["Offer ends", segs(offerEnds(m)), "offer_ends"]',
                      js_function("card"))
        self.assertIn('["Offer ends", "offer_ends"]', js_function("table"))
        self.assertIn("offer_ends:", PAGE)       # its (i) text


if __name__ == "__main__":
    unittest.main()
