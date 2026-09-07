"""Regression tests for help_content.py -- the plain-Python loader,
search, and markdown-subset renderer behind VeloCoder's built-in Help
(help_window.py owns turning this into a themed QTextBrowser document;
see tests/test_main.py's TestHelpWindow for that side).

Run with:  python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import help_content  # noqa: E402


class TestLoadTopics(unittest.TestCase):
    def test_loads_the_real_shipped_content(self):
        topics = help_content.load_topics()
        self.assertEqual(len(topics), 31)
        ids = [t.id for t in topics]
        self.assertEqual(len(ids), len(set(ids)), "topic ids must be unique")

    def test_every_topic_has_a_category_title_and_nonempty_body(self):
        for topic in help_content.load_topics():
            self.assertTrue(topic.category)
            self.assertTrue(topic.title)
            self.assertTrue(topic.body.strip())

    def test_welcome_is_the_first_topic(self):
        # help_window.py shows self._topics[0] on a fresh open -- this
        # pins that "Welcome to VeloCoder" is genuinely first, not an
        # implementation detail nothing actually depends on.
        topics = help_content.load_topics()
        self.assertEqual(topics[0].id, "welcome")

    def test_expected_categories_are_all_present(self):
        topics = help_content.load_topics()
        categories = {t.category for t in topics}
        self.assertEqual(categories, {
            "Getting Started", "Video", "Audio", "Using VeloCoder", "Expert", "Troubleshooting",
        })


class TestSearchTopics(unittest.TestCase):
    def setUp(self):
        self.topics = help_content.load_topics()

    def test_empty_query_returns_every_topic_unfiltered(self):
        self.assertEqual(help_content.search_topics(self.topics, ""), self.topics)

    def test_title_match_ranks_above_keyword_and_body_matches(self):
        hits = help_content.search_topics(self.topics, "quality")
        self.assertIn("choosing-quality", [t.id for t in hits])
        # A title match ("Choosing Quality", "Exact Quality") comes
        # before anything that only matched via keyword/body text.
        title_hit_ids = {t.id for t in hits if "quality" in t.title.lower()}
        first_non_title_index = next(
            (i for i, t in enumerate(hits) if t.id not in title_hit_ids), len(hits)
        )
        self.assertTrue(all(hits[i].id in title_hit_ids for i in range(first_non_title_index)))

    def test_case_insensitive(self):
        self.assertEqual(
            [t.id for t in help_content.search_topics(self.topics, "QUALITY")],
            [t.id for t in help_content.search_topics(self.topics, "quality")],
        )

    def test_no_match_returns_empty(self):
        self.assertEqual(help_content.search_topics(self.topics, "xyzzy_no_such_topic"), [])

    # The spec's own example queries -- each must return at least one
    # genuinely relevant topic, not just "something."
    def test_example_queries_return_useful_topics(self):
        expected_hit = {
            "10 bit": "color-depth",
            "quality": "choosing-quality",
            "smaller file": "choosing-quality",
            "audio": "audio-handling",
            "failed": "video-wont-convert",
            "h264": "codec",
            "h265": "codec",
            "size": "target-file-size",
            "stereo": "channels",
            "cpu": "processing",
            "automatic": "processing",
            "intel": "processing",
            "amd": "processing",
            "processing": "processing",
        }
        for query, expected_id in expected_hit.items():
            hits = help_content.search_topics(self.topics, query)
            self.assertIn(
                expected_id, [t.id for t in hits],
                f"query {query!r} should find topic {expected_id!r}, got {[t.id for t in hits]}",
            )

    def test_hardware_strongly_ranks_processing_not_just_incidentally_finds_it(self):
        # Previously this only worked because choosing-quality happened
        # to mention "hardware" in passing -- now Processing (Video
        # category, dedicated topic) is the actual home for this query,
        # and must rank first, not just appear somewhere in the results.
        hits = help_content.search_topics(self.topics, "hardware")
        self.assertTrue(hits)
        self.assertEqual(hits[0].id, "processing")

    def test_processing_topic_exists_in_the_video_category(self):
        topics = help_content.load_topics()
        processing = next((t for t in topics if t.id == "processing"), None)
        self.assertIsNotNone(processing)
        self.assertEqual(processing.category, "Video")
        self.assertEqual(processing.title, "Processing")

    def test_processing_topic_avoids_low_level_implementation_terms(self):
        # The Help system describes what the user needs to know, not
        # backend architecture -- VAAPI/render nodes/PCI IDs belong in
        # code comments, never in a help article a non-technical user
        # might actually read.
        processing = next(t for t in help_content.load_topics() if t.id == "processing")
        lowered = processing.body.lower()
        for forbidden in ("vaapi", "render node", "pci id", "drm"):
            self.assertNotIn(forbidden, lowered)

    def test_codec_help_no_longer_names_a_specific_backend_limitation(self):
        # Reported live: the old wording ("Intel and AMD hardware
        # encoding here always produces H.265") was tied to today's
        # specific backend implementation and would go stale the moment
        # that changes (e.g. a future h264_vaapi). The replacement talks
        # about what VeloCoder offers, not which vendor does what.
        codec = next(t for t in help_content.load_topics() if t.id == "codec")
        self.assertNotIn("Intel and AMD hardware encoding", codec.body)


class TestRenderMarkdownSubset(unittest.TestCase):
    def test_heading_becomes_h3(self):
        html = help_content.render_markdown_subset("## A Heading\n\nBody text.")
        self.assertIn("<h3>A Heading</h3>", html)

    def test_paragraph_lines_join_into_one_p(self):
        html = help_content.render_markdown_subset("Line one\nstill the same paragraph.")
        self.assertIn("<p>Line one still the same paragraph.</p>", html)

    def test_blank_line_starts_a_new_paragraph(self):
        html = help_content.render_markdown_subset("First.\n\nSecond.")
        self.assertIn("<p>First.</p>", html)
        self.assertIn("<p>Second.</p>", html)

    def test_bullet_list_becomes_ul_li(self):
        html = help_content.render_markdown_subset("- one\n- two")
        self.assertIn("<ul>", html)
        self.assertIn("<li>one</li>", html)
        self.assertIn("<li>two</li>", html)

    def test_bold_becomes_b_tag(self):
        html = help_content.render_markdown_subset("Some **bold** word.")
        self.assertIn("<b>bold</b>", html)

    def test_no_raw_markdown_syntax_survives_into_the_output(self):
        for topic in help_content.load_topics():
            html = help_content.render_markdown_subset(topic.body)
            self.assertNotIn("**", html)
            self.assertNotIn("\n- ", html)


class TestDeckText(unittest.TestCase):
    def test_leading_bold_only_paragraph_becomes_deck(self):
        html = help_content.render_markdown_subset("**A one-line summary.**\n\nMore detail here.")
        self.assertIn('<p class="deck">A one-line summary.</p>', html)
        self.assertIn("<p>More detail here.</p>", html)

    def test_bold_paragraph_with_trailing_text_is_not_a_deck(self):
        # The whole paragraph has to be the bold span -- "**Bold** and
        # more" is an ordinary paragraph that happens to start bold, not
        # a one-line summary.
        html = help_content.render_markdown_subset("**Bold** and more text.")
        self.assertNotIn('class="deck"', html)
        self.assertIn("<p><b>Bold</b> and more text.</p>", html)

    def test_only_the_first_paragraph_can_become_a_deck(self):
        # A later paragraph that happens to also be a single bold span
        # stays a plain paragraph -- deck text is specifically the
        # article's own opening summary, not "any lone bold paragraph".
        html = help_content.render_markdown_subset("First paragraph.\n\n**Second, all bold.**")
        self.assertNotIn('class="deck"', html)
        self.assertIn("<p><b>Second, all bold.</b></p>", html)

    def test_real_articles_with_deck_text_render_it(self):
        # A few real articles were given deck text as part of the Help
        # visual-upgrade pass -- confirms the syntax survives all the
        # way from the shipped .md file through to real HTML output.
        topics = {t.id: t for t in help_content.load_topics()}
        html = help_content.render_markdown_subset(topics["welcome"].body)
        self.assertIn('<p class="deck">', html)


class TestCallouts(unittest.TestCase):
    def test_callout_label_and_body(self):
        html = help_content.render_markdown_subset("> **Recommended**\n> Balanced works well.")
        self.assertIn('<div class="callout">', html)
        self.assertIn('<div class="callout-label"><b>Recommended</b></div>', html)
        self.assertIn('<div class="callout-body">Balanced works well.</div>', html)

    def test_callout_label_only_has_no_empty_body_div(self):
        html = help_content.render_markdown_subset("> **Note**")
        self.assertIn('<div class="callout-label"><b>Note</b></div>', html)
        self.assertNotIn("callout-body", html)

    def test_callout_is_distinct_from_a_following_paragraph(self):
        html = help_content.render_markdown_subset("> **Tip**\n> Do the thing.\n\nOrdinary paragraph.")
        self.assertIn('<div class="callout">', html)
        self.assertIn("<p>Ordinary paragraph.</p>", html)
        # The plain paragraph must land outside the callout div, not
        # merged into callout-body.
        self.assertNotIn("Ordinary paragraph.</div>", html)

    def test_real_articles_with_callouts_render_them(self):
        topics = {t.id: t for t in help_content.load_topics()}
        html = help_content.render_markdown_subset(topics["choosing-quality"].body)
        self.assertIn('<div class="callout">', html)


class TestImages(unittest.TestCase):
    def test_image_resolves_to_the_dark_variant_by_default(self):
        html = help_content.render_markdown_subset("![alt text](quick-start.png)")
        self.assertIn("quick-start-dark.png", html)
        self.assertIn('<img class="help-image"', html)
        self.assertIn('alt="alt text"', html)

    def test_image_resolves_to_the_light_variant_when_asked(self):
        html = help_content.render_markdown_subset("![alt text](quick-start.png)", dark=False)
        self.assertIn("quick-start-light.png", html)
        self.assertNotIn("quick-start-dark.png", html)

    def test_image_with_caption_renders_a_caption_div(self):
        html = help_content.render_markdown_subset('![alt](video-settings.png "A helpful caption")')
        self.assertIn('<div class="help-image-caption">A helpful caption</div>', html)

    def test_image_without_caption_has_no_caption_div(self):
        html = help_content.render_markdown_subset("![alt](video-settings.png)")
        self.assertNotIn("help-image-caption", html)

    def test_unknown_base_name_falls_back_to_the_literal_filename(self):
        # No "-dark"/"-light" variant exists for this name -- falls back
        # to the bare filename as given, for any image that only ever
        # ships one variant.
        html = help_content.render_markdown_subset("![alt](some-unthemed-icon.png)")
        self.assertIn("some-unthemed-icon.png", html)

    def test_image_path_never_escapes_images_dir(self):
        # A filename with directory components can never point outside
        # IMAGES_DIR -- Path(filename).name strips everything but the
        # bare name before it's ever joined to IMAGES_DIR.
        html = help_content.render_markdown_subset("![alt](../../etc/passwd)")
        self.assertIn(help_content.IMAGES_DIR.as_uri(), html)
        self.assertNotIn("etc/passwd", html)

    def test_real_articles_with_images_render_them(self):
        topics = {t.id: t for t in help_content.load_topics()}
        html = help_content.render_markdown_subset(topics["welcome"].body)
        self.assertIn('<img class="help-image"', html)


if __name__ == "__main__":
    unittest.main()
