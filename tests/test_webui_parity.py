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
        card = js_function("card")
        self.assertIn('oe.append(labelled("Offer ends", "offer_ends"), " ", segs(ends));',
                      card)
        self.assertNotIn('["Offer ends", segs(', card)   # not in a half-width column
        self.assertIn('label: "Offer ends", help: "offer_ends"', js_function("tableCols"))
        self.assertIn("offer_ends:", PAGE)       # its (i) text


class ShortStates(unittest.TestCase):
    def test_vast_short_forms_and_colours(self):
        src = js_function("vastState")
        for word, cls in (("not read", "st-mut"), ("unreadable", "st-bad"),
                          ("listed", "st-ok"), ("unlisted", "st-mut")):
            self.assertIn('["%s", "%s", ' % (word, cls), src)
        self.assertIn('["rented (" + r + ")", "st-vast", ', src)

    def test_paralon_short_forms_and_colours(self):
        src = js_function("paralonState")
        for word, cls in (("not read", "st-mut"), ("not found", "st-bad"),
                          ("rented", "st-para"), ("available", "st-ok"),
                          ("off: on Vast", "st-mut"), ("offline", "st-warn"),
                          ("paused", "st-mut")):
            self.assertIn('["%s", "%s", ' % (word, cls), src)
        # the order: a rental first, then open, then why it is closed
        order = [src.index('"%s"' % w) for w in
                 ("rented", "available", "off: on Vast", "offline", "paused")]
        self.assertEqual(order, sorted(order))

    def test_a_rental_never_names_the_renter(self):
        src = js_function("paralonState") + js_function("vastState")
        for field in ("paralon_rentals", "vast_running", "rental"):
            self.assertNotIn("." + field, src)

    def test_the_sentence_is_the_words_tip(self):
        self.assertIn("b.dataset.text = sentence", js_function("tipWord"))
        self.assertIn("b.dataset.text ? b.dataset.text :", js_function("showTip"))
        self.assertIn('closest("button.info, button.tipword")', js_function("infoOf"))
        # a tip word is a button: keyboard focus and tap open it too
        self.assertIn('el("button", "tipword"', js_function("tipWord"))

    def test_cards_and_table_use_them(self):
        self.assertIn('stateEl(vastState(m))', js_function("card"))
        self.assertIn('stateEl(paralonState(m))', js_function("card"))
        cols = js_function("tableCols")
        labels = re.findall(r'label: "([^"]+)"', cols)
        self.assertEqual(labels, ["Machine", "Mode", "Status", "Vast",
                                  "Vast price", "Offer ends", "Paralon",
                                  "Paralon price", "Next pass"])
        self.assertIn("onDemandEl(m)", cols)
        self.assertNotIn("vastShort", PAGE)
        self.assertNotIn("paralonShort", PAGE)

    def test_money_has_two_decimals_at_least(self):
        self.assertIn('v.toFixed(3).replace(/0$/, "")', js_function("money"))


class StackedPhoneCards(unittest.TestCase):
    def test_table_view_stacks_on_a_phone(self):
        self.assertIn('const PHONE = window.matchMedia("(max-width: 640px)");', PAGE)
        render = js_function("render")
        self.assertIn("PHONE.matches ? stacked(s) : table(s)", render)
        # no sideways-scrolling wrapper around the stacked cards
        self.assertIn('classList.toggle("table-wrap", !PHONE.matches)', render)
        self.assertIn('PHONE.addEventListener("change"', PAGE)

    def test_same_columns_primary_in_two_columns_rest_under_more(self):
        src = js_function("stacked")
        self.assertIn("tableCols(s)", src)                  # one column list
        self.assertIn("cols.filter((c) => c.primary)", src)
        self.assertIn('el("div", "mx")', src)
        self.assertIn('el("summary", null, "More")', src)
        self.assertIn("head.append(h, chipEl(m))", src)     # name + status
        self.assertIn("if (col.act) c.append(col.cell(m))", src)   # mode
        self.assertRegex(PAGE, r"\.mx \{ display: grid; grid-template-columns: "
                               r"minmax\(0, 1fr\) minmax\(0, 1fr\);")
        prim = re.findall(r'label: "([^"]+)"[^\n]*primary: true',
                          js_function("tableCols"))
        self.assertEqual(prim, ["Vast", "Vast price", "Paralon", "Paralon price"])

    def test_more_stays_open_across_refreshes(self):
        src = js_function("stacked")
        self.assertIn("d.open = moreOpen.has(m.vast_machine_id)", src)


class CoreVsAdvanced(unittest.TestCase):
    def core(self, name):
        m = re.search(r"const %s = new Set\(\[(.*?)\]\);" % name, PAGE, re.S)
        return set(re.findall(r'"([a-z_]+)"', m.group(1)))

    def test_core_keys_exist_and_the_rest_fold(self):
        from vpdual.settings import schema
        sch = schema()
        keys = {f["key"] for g in sch["groups"] for f in g["fields"]}
        core = self.core("CORE_SETTINGS")
        self.assertTrue(core <= keys, core - keys)
        self.assertEqual(core, {"armed", "on_paralon_rental", "poll_interval_s",
                                "idle_job_images", "notify_command",
                                "vastai_bin"})
        listing = self.core("CORE_LISTING")
        self.assertEqual(listing, {"price_gpu", "price_min_bid"})
        self.assertTrue(listing <= {f["key"] for f in sch["listing"]})

    def test_advanced_is_closed_counted_and_remembered(self):
        src = js_function("advanced")
        self.assertIn('el("details", "adv")', src)
        self.assertIn('"Advanced (" + rows.length + ")"', src)
        self.assertIn('"vpdual.adv." + id', src)
        self.assertIn('localStorage.getItem(key) === "1"', src)
        rs = js_function("renderSettings")
        self.assertIn("if (CORE_SETTINGS.has(f.key)) c.append(row); else adv.push(row);", rs)
        self.assertIn("if (adv.length) c.append(advanced(g.title, adv));", rs)
        self.assertIn('listingEditor(cfg, "vast_listing", "", true)', rs)
        # the per-machine overrides are not split (already folded)
        self.assertIn('listingEditor(m, p + ".vast_listing", "shared value")', rs)

    def test_split_listing_stays_inside_the_wrapper(self):
        src = js_function("listingEditor")
        self.assertIn("if (split && !CORE_LISTING.has(f.key)) adv.push(row);", src)
        self.assertIn('wrap.append(advanced("vast_listing", adv))', src)
        self.assertLess(src.index("wrap.append(advanced("),
                        src.index('wrap.addEventListener("input"'))

    def test_a_problem_opens_its_section(self):
        self.assertIn('spot.closest("details")', js_function("showErrors"))
        self.assertIn("d.open = true", js_function("showErrors"))


class ConfirmFlow(unittest.TestCase):
    def test_toasts_ok_goes_warn_and_err_stay(self):
        src = js_function("toast")
        self.assertIn('if (o.ms || kind === "ok") setTimeout(() => t.remove(), '
                      'o.ms || 8000);', src)
        self.assertIn("while (tray.children.length > 5)", src)
        self.assertIn('x.setAttribute("aria-label", "Dismiss")', src)
        self.assertIn('<div id="toasts" role="status" aria-live="polite"></div>', PAGE)
        self.assertNotIn('id="notice"', PAGE)
        self.assertNotIn('banner("notice"', PAGE)

    def test_mode_switch_one_apply_button_with_the_focus(self):
        ask = js_function("ask")
        self.assertIn('$("c-ok").textContent = "Apply to " + m.name;', ask)
        self.assertLess(ask.index('$("confirm").showModal();'),
                        ask.index('$("c-ok").focus();'))
        self.assertIn('toast(j.saved ? "ok" : "warn", j.message);',
                      js_function("confirmSwitch"))
        # the server side is unchanged: the same POST /api/mode
        self.assertIn('fetch("/api/mode"', js_function("confirmSwitch"))

    def test_settings_diff_then_one_apply_enter_reviews(self):
        rv = js_function("review")
        self.assertIn('"Apply to " + S.data.path.split(', rv)
        self.assertIn('"Check only"', rv)                  # dry run
        self.assertLess(rv.index('$("s-confirm").showModal();'),
                        rv.index('(arming ? $("s-yes") : $("s-save")).focus();'))
        self.assertIn('$("s-form").addEventListener("keydown"', PAGE)
        self.assertRegex(PAGE, r'e\.key !== "Enter"[^\n]*HTMLInputElement')


class TypedTier(unittest.TestCase):
    def test_arming_waits_for_yes(self):
        rv = js_function("review")
        self.assertIn('r.path === "armed"', rv)
        self.assertIn("r.new === true", rv)
        self.assertIn("!S.data.dry_run", rv)          # a dry run saves nothing
        self.assertIn('$("s-typed").hidden = !arming;', rv)
        self.assertIn('$("s-save").disabled = arming;', rv)
        self.assertIn('$("s-yes").value = "";', rv)   # never pre-filled
        self.assertIn('return $("s-yes").value.trim().toLowerCase() === "yes";',
                      js_function("typedYes"))
        self.assertIn('$("s-save").disabled = !typedYes();', PAGE)
        self.assertIn('if (!$("s-typed").hidden && !typedYes()) return;',
                      js_function("saveSettings"))
        self.assertIn('<input id="s-yes" type="text" autocomplete="off"', PAGE)

    def test_disarming_and_other_changes_stay_one_click(self):
        # only armed -> true is typed; the rest keep the one Apply button
        self.assertEqual(js_function("review").count("s-typed"), 1)


if __name__ == "__main__":
    unittest.main()
