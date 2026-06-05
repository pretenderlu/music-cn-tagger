import unittest
from unittest.mock import patch

import tagger


class FakeResponse:
    def __init__(self, results):
        self._results = results

    def raise_for_status(self):
        return None

    def json(self):
        return {"results": self._results}


class ITunesStorefrontTests(unittest.TestCase):
    def test_artist_name_match_rejects_partial_artist_names(self):
        self.assertTrue(tagger._itunes_artist_name_matches("Gareth.T", "Gareth.T"))
        self.assertFalse(tagger._itunes_artist_name_matches("Gareth.T", "Gareth"))

    def test_storefronts_add_us_as_secondary_once(self):
        self.assertEqual(tagger._itunes_storefronts(tagger.ScanOptions(country="tw")), ("tw", "us"))
        self.assertEqual(tagger._itunes_storefronts(tagger.ScanOptions(country="us")), ("us", "tw"))

    @patch("tagger.requests.get")
    def test_album_search_queries_current_country_then_us_and_dedupes(self, get):
        responses = {
            "tw": [
                {"wrapperType": "collection", "collectionId": 10, "collectionName": "台湾结果"},
                {"wrapperType": "collection", "collectionId": 20, "collectionName": "重复结果"},
            ],
            "us": [
                {"wrapperType": "collection", "collectionId": 20, "collectionName": "重复结果 US"},
                {"wrapperType": "collection", "collectionId": 30, "collectionName": "美国结果"},
            ],
        }

        def fake_get(url, params=None, **kwargs):
            return FakeResponse(responses[params["country"]])

        get.side_effect = fake_get

        albums = tagger.itunes_search_albums("周杰伦 七里香", tagger.ScanOptions(country="tw", limit=5))

        self.assertEqual([call.kwargs["params"]["country"] for call in get.mock_calls], ["tw", "us"])
        self.assertEqual([a["collectionId"] for a in albums], [10, 20, 30])

    @patch("tagger.requests.get")
    def test_album_detail_falls_back_to_us_when_primary_has_no_collection(self, get):
        responses = {
            "tw": [],
            "us": [
                {"wrapperType": "collection", "collectionId": 99, "collectionName": "七里香",
                 "artistName": "Jay Chou", "trackCount": 1},
                {"wrapperType": "track", "trackId": 100, "trackName": "我的地盘",
                 "artistName": "Jay Chou", "collectionId": 99,
                 "collectionName": "七里香", "trackNumber": 1, "discNumber": 1},
            ],
        }

        def fake_get(url, params=None, **kwargs):
            return FakeResponse(responses[params["country"]])

        get.side_effect = fake_get

        album, tracks = tagger.itunes_album_detail("99", tagger.ScanOptions(country="tw"))

        self.assertEqual([call.kwargs["params"]["country"] for call in get.mock_calls], ["tw", "us"])
        self.assertEqual(album["id"], "99")
        self.assertEqual(album["name"], "七里香")
        self.assertEqual([t["id"] for t in tracks], ["100"])

    @patch("tagger.requests.get")
    def test_album_detail_continues_when_storefront_has_collection_but_no_tracks(self, get):
        responses = {
            "us": [
                {"wrapperType": "collection", "collectionId": 1883630666,
                 "collectionName": "THE PROTEGE", "artistName": "Gareth.T", "trackCount": 12},
            ],
            "tw": [
                {"wrapperType": "collection", "collectionId": 1883630666,
                 "collectionName": "THE PROTEGE", "artistName": "Gareth.T", "trackCount": 12},
                {"wrapperType": "track", "trackId": 1883630669, "trackName": "国际孤独等级",
                 "artistName": "Gareth.T", "collectionId": 1883630666,
                 "collectionName": "THE PROTEGE", "trackNumber": 1, "discNumber": 1},
            ],
        }

        def fake_get(url, params=None, **kwargs):
            return FakeResponse(responses[params["country"]])

        get.side_effect = fake_get

        album, tracks = tagger.itunes_album_detail("1883630666", tagger.ScanOptions(country="us"))

        self.assertEqual([call.kwargs["params"]["country"] for call in get.mock_calls], ["us", "tw"])
        self.assertEqual(album["storefront"], "tw")
        self.assertEqual([t["name"] for t in tracks], ["国际孤独等级"])

    @patch("tagger.requests.get")
    def test_artist_album_lookup_merges_song_search_collections(self, get):
        def fake_get(url, params=None, **kwargs):
            if params.get("entity") == "musicArtist":
                return FakeResponse([
                    {"wrapperType": "artist", "artistId": 1, "artistName": "Gareth.T"},
                ])
            if params.get("entity") == "album":
                return FakeResponse([
                    {"wrapperType": "artist", "artistId": 1, "artistName": "Gareth.T"},
                    {"wrapperType": "collection", "collectionId": 10,
                     "collectionName": "to be honest", "artistName": "Gareth.T", "trackCount": 9},
                ])
            if params.get("entity") == "song" and params.get("attribute") == "artistTerm":
                return FakeResponse([
                    {"wrapperType": "track", "trackId": 6769327013, "trackName": "玻璃",
                     "artistName": "Gareth.T", "collectionId": 6769327003,
                     "collectionName": "玻璃 - Single", "trackCount": 1},
                ])
            return FakeResponse([])

        get.side_effect = fake_get

        albums = tagger._itunes_lookup_artist_albums("Gareth.T", tagger.ScanOptions(country="us"))

        self.assertEqual([a["collectionId"] for a in albums], [10, 6769327003])
        self.assertEqual(albums[1]["collectionName"], "玻璃 - Single")
        self.assertEqual(albums[1]["trackCount"], 1)

    @patch("tagger.requests.get")
    def test_artist_album_lookup_merges_generic_song_collections_across_catalog_storefronts(self, get):
        def fake_get(url, params=None, **kwargs):
            if params.get("entity") == "musicArtist":
                return FakeResponse([
                    {"wrapperType": "artist", "artistId": 1, "artistName": "Gareth.T"},
                ])
            if params.get("entity") == "album":
                return FakeResponse([
                    {"wrapperType": "artist", "artistId": 1, "artistName": "Gareth.T"},
                ])
            if params.get("entity") == "song" and params.get("attribute") == "artistTerm":
                return FakeResponse([])
            if params.get("entity") == "song" and params.get("country") == "cn":
                return FakeResponse([
                    {"wrapperType": "track", "trackId": 6769327013, "trackName": "玻璃",
                     "artistName": "Gareth.T", "collectionId": 6769327003,
                     "collectionName": "玻璃 - Single", "trackCount": 1},
                ])
            return FakeResponse([])

        get.side_effect = fake_get

        albums = tagger._itunes_lookup_artist_albums("Gareth.T", tagger.ScanOptions(country="us"))

        self.assertEqual([a["collectionId"] for a in albums], [6769327003])
        song_calls = [
            call.kwargs["params"]
            for call in get.mock_calls
            if call.kwargs["params"].get("entity") == "song"
        ]
        self.assertIn("cn", [params["country"] for params in song_calls])

    @patch("tagger.requests.get")
    def test_artist_song_album_candidates_preserve_apple_relevance_order(self, get):
        first_response = [
            {"wrapperType": "track", "trackId": 1, "trackName": "B Song",
             "artistName": "Gareth.T", "collectionId": 20,
             "collectionName": "B Song - Single", "trackCount": 1},
            {"wrapperType": "track", "trackId": 2, "trackName": "A Song",
             "artistName": "Gareth.T", "collectionId": 10,
             "collectionName": "A Song - Single", "trackCount": 1},
        ]
        seen_song_call = False

        def fake_get(url, params=None, **kwargs):
            nonlocal seen_song_call
            if params.get("entity") == "song" and not seen_song_call:
                seen_song_call = True
                return FakeResponse(first_response)
            return FakeResponse([])

        get.side_effect = fake_get

        candidates = tagger._itunes_artist_song_album_candidates("Gareth.T", tagger.ScanOptions(country="us"))

        self.assertEqual([album["id"] for album, _tracks in candidates], ["20", "10"])


if __name__ == "__main__":
    unittest.main()
