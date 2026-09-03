"""Public community feed used by Infernux Hub."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


COMMUNITY_ORIGIN = "https://infernux-engine.discourse.group"
HOT_TOPICS_URL = f"{COMMUNITY_ORIGIN}/top.json?period=weekly"


@dataclass(frozen=True)
class HotTopic:
    title: str
    url: str
    replies: int
    views: int
    likes: int


def parse_hot_topics(document: object, *, limit: int = 6) -> list[HotTopic]:
    if not isinstance(document, dict):
        raise ValueError("Community response must be an object")
    topic_list = document.get("topic_list")
    if not isinstance(topic_list, dict) or topic_list.get("filter") != "top":
        raise ValueError("Community response is not a top-topic feed")
    topics = topic_list.get("topics")
    if not isinstance(topics, list):
        raise ValueError("Community response has no topic list")

    result: list[HotTopic] = []
    for topic in topics[:limit]:
        if not isinstance(topic, dict):
            raise ValueError("Community topic must be an object")
        title = topic.get("title")
        slug = topic.get("slug")
        topic_id = topic.get("id")
        posts = topic.get("posts_count")
        views = topic.get("views")
        likes = topic.get("like_count")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(slug, str)
            or not slug
            or not isinstance(topic_id, int)
            or not isinstance(posts, int)
            or not isinstance(views, int)
            or not isinstance(likes, int)
        ):
            raise ValueError("Community topic does not match the current contract")
        result.append(
            HotTopic(
                title=title.strip(),
                url=f"{COMMUNITY_ORIGIN}/t/{slug}/{topic_id}",
                replies=max(0, posts - 1),
                views=views,
                likes=likes,
            )
        )
    return result


def fetch_hot_topics(*, limit: int = 6) -> list[HotTopic]:
    request = urllib.request.Request(
        HOT_TOPICS_URL,
        headers={"Accept": "application/json", "User-Agent": "InfernuxHub-Community"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        document = json.loads(response.read().decode("utf-8"))
    return parse_hot_topics(document, limit=limit)


__all__ = ["COMMUNITY_ORIGIN", "HOT_TOPICS_URL", "HotTopic", "fetch_hot_topics", "parse_hot_topics"]
