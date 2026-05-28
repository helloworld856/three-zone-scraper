import unittest
from unittest.mock import MagicMock, patch
import os
import tempfile

from src.platforms.youtube.comments import (
    extract_video_id,
    format_youtube_duration,
    parse_video_entries,
    fetch_video_metrics,
)

class TestYouTubeMetrics(unittest.TestCase):

    def test_extract_video_id(self):
        self.assertEqual(extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_video_id("dQw4w9WgXcQ"), "") # invalid url format

    def test_format_youtube_duration(self):
        self.assertEqual(format_youtube_duration("PT1H2M10S"), "1:02:10")
        self.assertEqual(format_youtube_duration("PT4M1S"), "4:01")
        self.assertEqual(format_youtube_duration("PT3S"), "0:03")
        self.assertEqual(format_youtube_duration("PT1H10S"), "1:00:10")
        self.assertEqual(format_youtube_duration("P1D"), "") # We only support PT

    def test_parse_video_entries(self):
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as f:
            f.write("https://www.youtube.com/watch?v=A\nhttps://youtu.be/B\n# comment\nhttps://www.youtube.com/watch?v=A")
            temp_path = f.name
        
        try:
            entries = parse_video_entries(temp_path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["视频ID"], "A")
            self.assertEqual(entries[1]["视频ID"], "B")
            self.assertEqual(entries[0]["有效行数"], 3)
            self.assertEqual(entries[0]["重复行数"], 1)
        finally:
            os.remove(temp_path)

    @patch("src.platforms.youtube.comments.build")
    def test_fetch_video_metrics(self, mock_build):
        mock_youtube = MagicMock()
        mock_videos = MagicMock()
        mock_list = MagicMock()
        
        mock_youtube.videos.return_value = mock_videos
        mock_videos.list.return_value = mock_list
        mock_list.execute.return_value = {
            "items": [
                {
                    "id": "vid1",
                    "snippet": {
                        "title": "Test Video 1",
                        "channelTitle": "Test Channel",
                        "publishedAt": "2023-01-01T12:00:00Z",
                        "description": "Short description"
                    },
                    "statistics": {
                        "viewCount": "1000",
                        "likeCount": "100",
                        "commentCount": "10"
                    },
                    "contentDetails": {
                        "duration": "PT1M15S"
                    }
                }
            ]
        }
        
        metrics = fetch_video_metrics(mock_youtube, ["vid1"])
        self.assertIn("vid1", metrics)
        self.assertEqual(metrics["vid1"]["标题"], "Test Video 1")
        self.assertEqual(metrics["vid1"]["频道名称"], "Test Channel")
        self.assertEqual(metrics["vid1"]["发布日期"], "2023-01-01 12:00:00")
        self.assertEqual(metrics["vid1"]["视频时长"], "1:15")
        self.assertEqual(metrics["vid1"]["视频简介"], "Short description")
        self.assertEqual(metrics["vid1"]["播放量"], "1000")
        self.assertEqual(metrics["vid1"]["点赞数"], "100")
        self.assertEqual(metrics["vid1"]["评论数"], "10")

if __name__ == '__main__':
    unittest.main()


