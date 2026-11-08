import unittest
from unittest import mock

from app import analyze, config as settings, db, extract, vault


class UtilsTest(unittest.TestCase):
    def test_url_normalization_strips_tracking(self):
        url = "https://Example.com/Post/?utm_source=tg&igsh=abc&id=7#frag"
        self.assertEqual(db.normalize_url(url), "https://example.com/Post?id=7")
        self.assertEqual(db.url_key("https://example.com/Post"), db.url_key("https://example.com/Post/"))

    def test_kind_of(self):
        self.assertEqual(extract.kind_of("https://youtu.be/abc"), "youtube")
        self.assertEqual(extract.kind_of("https://www.instagram.com/reel/abc/"), "instagram")
        self.assertEqual(extract.kind_of("https://habr.com/ru/articles/1/"), "article")

    def test_extract_urls_dedupes_and_reads_entities(self):
        text = "смотри https://a.com/x и https://a.com/x, ещё https://b.com/y."
        entities = [{"type": "text_link", "url": "https://c.com/z"}]
        self.assertEqual(extract.extract_urls(text, entities),
                         ["https://c.com/z", "https://a.com/x", "https://b.com/y"])

    def test_vtt_to_text(self):
        vtt = ("WEBVTT\n\n00:00.000 --> 00:02.000\nПривет\n\n"
               "00:02.000 --> 00:04.000\nПривет\nмир\n")
        self.assertEqual(extract._vtt_text(vtt), "Привет мир")

    def test_slugify(self):
        self.assertEqual(vault.slugify("Что такое нейросеть?"), "chto-takoe-neyroset")
        self.assertEqual(vault.slugify(""), "item")

    def test_instagram_urls_collapse_to_one_key(self):
        variants = [
            "https://www.instagram.com/reel/DfPVvisN11Z?psln=MXNramNsOTFzZ2R0NA%3D%3D",
            "https://www.instagram.com/reels/DfPVvisN11Z/",
            "https://instagram.com/reel/DfPVvisN11Z?igsh=abc&xtok=zzz",
        ]
        self.assertEqual(len({db.url_key(u) for u in variants}), 1)
        self.assertEqual(db.url_key(variants[0]), "https://www.instagram.com/reel/DfPVvisN11Z")

    def test_content_weight_ignores_boilerplate(self):
        thin = "Video by artemvoidd\n\nAgent Desktop: computer use\n#ии #нейросети #ai"
        self.assertLess(extract.content_weight(thin), 60)
        full = "Video by x\n\n" + "Подробный разбор того, как работает агент. " * 10
        self.assertGreater(extract.content_weight(full), 200)

    def test_thin_content_is_not_sent_to_model(self):
        data = {"title": "Agent Desktop", "site": "Instagram", "kind": "instagram",
                "url": "https://www.instagram.com/reel/x",
                "text": "Video by artemvoidd\n\nAgent Desktop: computer use\n#ии #ai"}
        analysis = analyze.analyze(data)
        self.assertTrue(analysis["degraded"])
        self.assertEqual(analysis["model"], "no-text")
        self.assertEqual(analysis["quality"], 0)
        self.assertEqual(analysis["key_points"], [])
        self.assertIn("мало текста", analysis["warning"])

    def test_extraction_warning_survives(self):
        data = {"title": "", "site": "Instagram", "kind": "instagram", "url": "u",
                "text": "", "warning": "yt-dlp: HTTP Error 429"}
        analysis = analyze.analyze(data)
        self.assertIn("429", analysis["warning"])
        self.assertIn("пустое содержимое", analysis["warning"])

    def test_ytdlp_errors_are_translated(self):
        self.assertIn("нет видео", extract._friendly_error(
            "ERROR: [Instagram] DfSAXZHRliR: There is no video in this post"))
        self.assertIn("429", extract._friendly_error("Unable to download webpage: HTTP Error 429"))
        self.assertEqual(extract._friendly_error("что-то своё"), "что-то своё")

    def test_thin_video_is_transcribed(self):
        thin = {"title": "Agent Desktop", "description": "Video by x", "text": "Video by x",
                "duration": 42, "kind": "instagram", "site": "Instagram", "author": "x",
                "published": "", "thumb": "", "transcript": ""}
        with mock.patch.object(extract, "_ytdlp", return_value=dict(thin)), \
             mock.patch.object(extract, "_instagram_oembed", return_value={}), \
             mock.patch("app.asr.transcribe", return_value={
                 "text": "Расшифровка ролика про агента, " * 20, "seconds": 42, "error": ""}):
            data = extract.fetch("https://www.instagram.com/reel/DfPVvisN11Z")
        self.assertTrue(data["asr"])
        self.assertEqual(len(data["transcript"]), len("Расшифровка ролика про агента, " * 20))
        self.assertIn("Расшифровка ролика", data["text"])
        self.assertEqual(data["duration"], 42)
        self.assertEqual(analyze.substance(data["text"]) > analyze.MIN_SUBSTANCE, True)

    def test_asr_failure_keeps_reason(self):
        thin = {"title": "", "description": "", "text": "", "duration": 0, "kind": "instagram",
                "site": "Instagram", "author": "", "published": "", "thumb": "", "transcript": ""}
        with mock.patch.object(extract, "_ytdlp", return_value=dict(thin)), \
             mock.patch.object(extract, "_instagram_oembed", return_value={}), \
             mock.patch("app.asr.transcribe", return_value={
                 "text": "", "seconds": 0, "error": "речь в ролике не распознана"}):
            data = extract.fetch("https://www.instagram.com/reel/DfPVvisN11Z")
        self.assertNotIn("asr", data)
        self.assertIn("речь в ролике не распознана", data["warning"])

    def test_long_video_skips_transcription(self):
        thin = {"title": "", "description": "", "text": "", "duration": 3600, "kind": "youtube",
                "site": "YouTube", "author": "", "published": "", "thumb": "", "transcript": ""}
        with mock.patch.object(extract, "_ytdlp", return_value=dict(thin)), \
             mock.patch("app.asr.transcribe") as transcriber:
            extract.fetch("https://www.youtube.com/watch?v=abc")
        transcriber.assert_not_called()

    def test_instagram_product_images_reads_all_slides(self):
        photo = {"image_versions2": {"candidates": [
            {"url": "https://cdn/1-big.jpg"}, {"url": "https://cdn/1-small.jpg"}]}}
        carousel = {"carousel_media": [
            photo,
            {"image_versions2": {"candidates": [{"url": "https://cdn/2-big.jpg"}]}},
            {"video_versions": [{"url": "https://cdn/v.mp4"}],
             "image_versions2": {"candidates": [{"url": "https://cdn/3-poster.jpg"}]}},
        ]}
        self.assertEqual(extract._product_images(photo), ["https://cdn/1-big.jpg"])
        self.assertEqual(extract._product_images(carousel),
                         ["https://cdn/1-big.jpg", "https://cdn/2-big.jpg", "https://cdn/3-poster.jpg"])
        self.assertEqual(extract._product_images({}), [])

    def test_photo_post_falls_back_to_cover(self):
        """yt-dlp не отдал слайды (429/пустой ответ) — берём обложку из oEmbed."""
        cover = "https://scontent/cover.jpg"
        oembed = {"title": "", "author": "a", "description": "", "duration": 0, "published": "",
                  "thumb": cover, "transcript": "", "site": "Instagram", "kind": "instagram",
                  "text": "", "caption": ""}
        with mock.patch.object(extract, "_ytdlp",
                               side_effect=RuntimeError("There is no video in this post")), \
             mock.patch.object(extract, "_instagram_oembed", return_value=dict(oembed)), \
             mock.patch("app.asr.transcribe") as transcriber:
            data = extract.fetch("https://www.instagram.com/p/abc")
        transcriber.assert_not_called()
        self.assertEqual(data["images"], [cover])

    def test_carousel_images_survive_ytdlp_crash(self):
        """Картинки уже перехвачены, когда yt-dlp падает на посте без видео."""
        def fake_ytdlp(url, images=None):
            images.extend(["https://cdn/1.jpg", "https://cdn/2.jpg"])
            raise RuntimeError("ERROR: No video formats found")

        with mock.patch.object(extract, "_ytdlp", side_effect=fake_ytdlp), \
             mock.patch.object(extract, "_instagram_oembed", return_value={}), \
             mock.patch("app.asr.transcribe") as transcriber:
            data = extract.fetch("https://www.instagram.com/p/abc")
        transcriber.assert_not_called()
        self.assertEqual(data["images"], ["https://cdn/1.jpg", "https://cdn/2.jpg"])
        self.assertIn("фото или карусель", data["warning"])

    def test_telegram_photos_from_page(self):
        page = """<a class="tgme_widget_message_photo_wrap" href="https://t.me/x/1"
                     style="background-image:url('https://cdn/tg-1.jpg')"></a>
                  <a class="tgme_widget_message_photo_wrap" href="https://t.me/x/1"
                     style="background-image:url('https://cdn/tg-2.jpg')"></a>"""
        self.assertEqual(extract._telegram_images(page),
                         ["https://cdn/tg-1.jpg", "https://cdn/tg-2.jpg"])
        self.assertEqual(extract._telegram_images('<meta property="og:image" content="https://cdn/og.jpg">'),
                         ["https://cdn/og.jpg"])
        self.assertEqual(extract._telegram_images("<html></html>"), [])

    def test_card_lists_saved_photos(self):
        item = {"id": 7, "url": "https://x/y", "created": "2026-11-08T10:00:00Z",
                "file_path": None}
        data = {"title": "T", "site": "Instagram", "kind": "instagram",
                "url": "https://x/y", "image_paths": ["attachments/stem/01.jpg",
                                                      "attachments/stem/02.jpg"]}
        analysis = {"title_ru": "Заголовок", "tags": ["тег"], "category": "Другое",
                    "quality": 3, "degraded": False, "summary": "кратко"}
        card = vault.render(item, data, analysis)
        self.assertIn("photos: 2", card)
        self.assertIn("## Фото", card)
        self.assertIn("![Фото 1 из 2](attachments/stem/01.jpg)", card)
        self.assertIn("![Фото 2 из 2](attachments/stem/02.jpg)", card)

    def test_card_without_photos_has_no_section(self):
        item = {"id": 7, "url": "https://x/y", "created": "2026-11-08T10:00:00Z",
                "file_path": None}
        card = vault.render(item, {"title": "T", "url": "https://x/y"},
                            {"title_ru": "Заголовок", "tags": [], "category": "Другое",
                             "quality": 3, "degraded": False})
        self.assertNotIn("photos:", card)
        self.assertNotIn("## Фото", card)

    def test_save_images_writes_files_and_drops_stale(self):
        import tempfile
        from pathlib import Path

        import httpx as _httpx

        class FakeResponse:
            def __init__(self, content, content_type):
                self.content = content
                self.headers = {"content-type": content_type}

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, mapping):
                self.mapping = mapping
                self.closed = False

            def get(self, url, headers=None):
                if url not in self.mapping:
                    raise _httpx.HTTPError("boom")
                return FakeResponse(*self.mapping[url])

            def close(self):
                self.closed = True

        jpeg = b"\xff\xd8\xff\xe0" + b"0" * 32
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        folder_name = settings.IMAGES_DIR_NAME
        with tempfile.TemporaryDirectory() as folder:
            card = Path(folder) / "2026-11-08-x-7.md"
            card.write_text("", encoding="utf-8")
            stale = card.parent / folder_name / card.stem / "09.jpg"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(jpeg)
            client = FakeClient({"https://cdn/a.jpg": (jpeg, "image/jpeg"),
                                 "https://cdn/b": (png, "image/png")})
            saved, errors = vault.save_images(
                card, ["https://cdn/a.jpg", "https://cdn/b", "https://cdn/broken"], client=client)
            self.assertEqual(saved, [f"{folder_name}/{card.stem}/01.jpg",
                                     f"{folder_name}/{card.stem}/02.png"])
            self.assertEqual(len(errors), 1)
            self.assertFalse(stale.exists())
            self.assertTrue((card.parent / saved[0]).exists())


    def test_trash_card_moves_photo_folder(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as vault_dir, \
                tempfile.TemporaryDirectory() as data_dir:
            with mock.patch.object(settings, "LIBRARY_DIR", Path(vault_dir)), \
                    mock.patch.object(settings, "DATA_DIR", Path(data_dir)):
                card = Path(vault_dir) / "2026-11" / "x.md"
                card.parent.mkdir(parents=True)
                card.write_text("card", encoding="utf-8")
                photos = card.parent / settings.IMAGES_DIR_NAME / "x"
                photos.mkdir(parents=True)
                (photos / "01.jpg").write_bytes(b"\xff\xd8\xff")
                vault.trash_card({"id": 5, "file_path": "2026-11/x.md"})
                self.assertFalse(card.exists())
                self.assertFalse(photos.exists())
                self.assertTrue((Path(data_dir) / "trash").is_dir())


if __name__ == "__main__":
    unittest.main()
