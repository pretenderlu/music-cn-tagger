import unittest
from unittest.mock import patch

import app


class ITunesPreviewTests(unittest.TestCase):
    @patch("app.tg._itunes_lookup_artist_albums", return_value=[])
    @patch("app.tg.itunes_album_detail")
    @patch("app.tg.itunes_search_albums")
    def test_preview_lists_matched_album_even_when_album_and_artist_are_not_chinese(
        self, itunes_search_albums, itunes_album_detail, _itunes_lookup_artist_albums
    ):
        itunes_search_albums.return_value = [
            {
                "wrapperType": "collection",
                "collectionId": 100,
                "collectionName": "THE PROTEGE",
                "artistName": "Gareth.T",
                "trackCount": 2,
                "releaseDate": "2024-01-01T00:00:00Z",
            }
        ]
        itunes_album_detail.return_value = (
            {"id": "100", "name": "THE PROTEGE", "artist_name": "Gareth.T"},
            [
                {"no": 1, "cd": 1, "name": "下一句", "artist_name": "Gareth.T"},
                {"no": 2, "cd": 1, "name": "紧急联络人", "artist_name": "Gareth.T"},
            ],
        )

        client = app.app.test_client()
        response = client.post("/api/itunes-preview", json={
            "album": "THE PROTEGE",
            "artist": "Gareth.T",
            "country": "tw",
        })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["candidates"]), 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["name"], "THE PROTEGE")
        self.assertEqual(candidate["artist"], "Gareth.T")
        self.assertEqual([track["name"] for track in candidate["tracks"]], ["下一句", "紧急联络人"])


if __name__ == "__main__":
    unittest.main()
