from __future__ import annotations

from app.story_hash import story_hash_for_news_item, story_hash_for_rss_entry


def test_story_hash_for_news_item_matches_title_tokens():
    item = {"title": "Trump announces tariffs on China trade deal", "summary": ""}
    a = story_hash_for_news_item(item)
    b = story_hash_for_news_item(dict(item))
    assert len(a) == 16
    assert a == b


def test_story_hash_rss_combines_title_description():
    h = story_hash_for_rss_entry("Fed holds rates steady", "Powell comments on inflation")
    assert len(h) == 16
