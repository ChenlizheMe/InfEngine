from __future__ import annotations

import pytest

from community_feed import parse_hot_topics


def test_parse_hot_topics_builds_public_topic_urls():
    topics = parse_hot_topics(
        {
            "topic_list": {
                "filter": "top",
                "topics": [
                    {
                        "id": 21,
                        "title": "Renderer notes",
                        "slug": "renderer-notes",
                        "posts_count": 4,
                        "views": 82,
                        "like_count": 6,
                    }
                ],
            }
        }
    )

    assert topics[0].title == "Renderer notes"
    assert topics[0].url.endswith("/t/renderer-notes/21")
    assert topics[0].replies == 3
    assert topics[0].views == 82
    assert topics[0].likes == 6


def test_parse_hot_topics_rejects_a_non_top_feed():
    with pytest.raises(ValueError, match="top-topic"):
        parse_hot_topics({"topic_list": {"filter": "latest", "topics": []}})


def test_parse_hot_topics_rejects_incomplete_topics():
    with pytest.raises(ValueError, match="current contract"):
        parse_hot_topics(
            {
                "topic_list": {
                    "filter": "top",
                    "topics": [{"id": 1, "title": "Incomplete"}],
                }
            }
        )
