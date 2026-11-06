import unittest

from app.youtube import evaluate


class YouTubeTests(unittest.TestCase):
    def evidence(self, **changes):
        return {"youtube_probe_version": "2", "youtube_watch": "200 0.5 100000",
                "youtube_api": "200 0.2 1000", "youtube_watch_valid": "1",
                "youtube_api_valid": "1", **changes}

    def test_actual_client_playback_overrides_router_timeouts(self):
        result = evaluate(self.evidence(youtube_watch="000 8 0", youtube_api="000 6 0",
                                        youtube_active_bytes="131072", youtube_active_clients="1"))
        self.assertEqual(result[0], "ok")
        self.assertIn("устройства получают видео", result[1])

    def test_old_connection_totals_or_small_transfers_are_not_playback(self):
        for changes in ({"youtube_active_bytes": "64000", "youtube_active_clients": "1"},
                        {"youtube_active_bytes": "1000000", "youtube_active_clients": "0"}):
            self.assertEqual(evaluate(self.evidence(**changes))[0], "unknown")

    def test_real_video_chunk_passes_even_if_metadata_api_is_unavailable(self):
        result = evaluate(self.evidence(youtube_api="000 6 0", youtube_media="206 0.4 65536 video/mp4",
                                        youtube_media_exit="0"))
        self.assertEqual(result[0], "ok")
        self.assertIn("фрагмент видео", result[1])

    def test_http_error_html_empty_or_incomplete_transfer_is_not_playback(self):
        for media, exit_code in (("403 0.1 65536 video/mp4", "0"),
                                 ("200 0.1 65536 text/html", "0"),
                                 ("206 0.1 0 video/mp4", "0"),
                                 ("206 8 65536 video/mp4", "28"),
                                 ("200 0.1 2000000 video/mp4", "0")):
            with self.subTest(media=media):
                self.assertEqual(evaluate(self.evidence(youtube_media=media, youtube_media_exit=exit_code))[0], "unknown")

    def test_bot_challenge_and_unplayable_test_video_do_not_report_an_outage(self):
        for changes in ({"youtube_watch": "429 0.1 0"},
                        {"youtube_player_status": "LOGIN_REQUIRED"},
                        {"youtube_player_status": "UNPLAYABLE"},
                        {"youtube_media_candidate": "1", "youtube_media": "403 0.1 30 text/plain"}):
            self.assertEqual(evaluate(self.evidence(**changes))[0], "unknown")

    def test_no_video_url_does_not_turn_page_and_metadata_into_verified_playback(self):
        self.assertEqual(evaluate(self.evidence(youtube_player_status="OK"))[0], "unknown")

    def test_actual_network_and_cdn_failures_remain_failures(self):
        for changes in ({"youtube_watch": "000 8 0", "youtube_api": "000 6 0"},
                        {"youtube_watch": "503 1 20", "youtube_api": "502 1 20"},
                        {"youtube_media_candidate": "1", "youtube_media": "000 8 0 none"}):
            self.assertEqual(evaluate(self.evidence(**changes))[0], "fail")

    def test_old_agents_and_invalid_measurements_cannot_claim_playback(self):
        self.assertEqual(evaluate({"pool_youtube": "https://www.youtube.com/ 000 10"})[0], "unknown")
        self.assertEqual(evaluate(self.evidence(youtube_active_bytes="nan", youtube_active_clients="1"))[0], "unknown")


if __name__ == "__main__":
    unittest.main()
