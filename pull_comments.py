#!/usr/bin/env python3
"""Pull YouTube comments for The Ansible Playbook channel and publish as RSS.

Stateless — the feed.xml in the GitHub Pages repo is the only state.
Supports tiered scraping: --tier hot|warm|cold|all
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring, parse as parse_xml

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

sys.stdout.reconfigure(line_buffering=True)

CHANNEL_URL = "https://www.youtube.com/@AnsibleAutomation/videos"
FEED_REPO = "git@github.com:ansible-tmm/youtube-comments-feed.git"
FEED_URL = "https://ansible-tmm.github.io/youtube-comments-feed/feed.xml"
DELAY_BETWEEN_VIDEOS = 2
MAX_FEED_ITEMS = 500

TIERS = {
    "hot": (0, 10),
    "warm": (10, 50),
    "cold": (50, None),
    "all": (0, None),
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tier", choices=TIERS.keys(), default="all")
    p.add_argument("--feed-repo-dir", type=Path, default=None,
                   help="Path to local feed repo clone (default: temp dir)")
    return p.parse_args()


def discover_videos(tier):
    start, end = TIERS[tier]
    print(f"Discovering videos from channel ...")
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp",
         "--flat-playlist", "--print", "%(id)s\t%(title)s",
         CHANNEL_URL],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"Error discovering videos: {result.stderr}")
        sys.exit(1)

    videos = []
    for line in result.stdout.strip().split("\n"):
        if "\t" in line:
            vid, title = line.split("\t", 1)
            videos.append({"id": vid, "title": title})

    # yt-dlp returns newest first from the /videos tab
    sliced = videos[start:end]
    print(f"Found {len(videos)} total, tier '{tier}' selects [{start}:{end or ''}] → {len(sliced)} videos")
    return sliced


def fetch_comments(video_id, work_dir):
    out_template = str(work_dir / video_id)
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp",
         "--skip-download", "--write-comments",
         "--no-write-thumbnail", "--no-write-description",
         "-o", out_template,
         f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print(f"  Warning: yt-dlp error for {video_id}: {result.stderr[:200]}")
        return []

    info_file = work_dir / f"{video_id}.info.json"
    if not info_file.exists():
        return []

    with open(info_file) as f:
        info = json.load(f)

    rows = []
    for c in info.get("comments") or []:
        rows.append({
            "comment_id": c.get("id", ""),
            "author": c.get("author", ""),
            "text": c.get("text", ""),
            "likes": c.get("like_count", 0),
            "timestamp": c.get("timestamp"),
            "is_reply": c.get("parent") not in (None, "root"),
            "video_id": video_id,
            "video_title": info.get("title", ""),
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return rows


def load_existing_guids(feed_file):
    if not feed_file.exists():
        return set()
    try:
        tree = parse_xml(feed_file)
        return {el.text for el in tree.iter("guid") if el.text}
    except Exception:
        return set()


def build_feed(existing_feed_file, new_comments):
    existing_items = []
    if existing_feed_file.exists():
        try:
            tree = parse_xml(existing_feed_file)
            for item in tree.iter("item"):
                existing_items.append({
                    "title": (item.find("title").text or "") if item.find("title") is not None else "",
                    "link": (item.find("link").text or "") if item.find("link") is not None else "",
                    "description": (item.find("description").text or "") if item.find("description") is not None else "",
                    "guid": (item.find("guid").text or "") if item.find("guid") is not None else "",
                    "pubDate": (item.find("pubDate").text or "") if item.find("pubDate") is not None else "",
                })
        except Exception:
            pass

    existing_guids = {i["guid"] for i in existing_items}

    for c in new_comments:
        if c["comment_id"] in existing_guids:
            continue
        prefix = "Reply: " if c["is_reply"] else ""
        pub_date = ""
        if c.get("timestamp"):
            dt = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc)
            pub_date = format_datetime(dt)
        likes = f" [{c['likes']} likes]" if c.get("likes") else ""
        existing_items.append({
            "title": f"{prefix}{c['author']} on \"{c['video_title']}\"",
            "link": c["video_url"],
            "description": f"{c['text']}{likes}",
            "guid": c["comment_id"],
            "pubDate": pub_date,
        })

    existing_items.sort(
        key=lambda i: i.get("pubDate", ""),
        reverse=True,
    )
    existing_items = existing_items[:MAX_FEED_ITEMS]

    rss = Element("rss", version="2.0")
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "The Ansible Playbook — YouTube Comments"
    SubElement(channel, "link").text = "https://www.youtube.com/@AnsibleAutomation"
    SubElement(channel, "description").text = "New comments on The Ansible Playbook YouTube channel"
    SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for item_data in existing_items:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = item_data["title"]
        SubElement(item, "link").text = item_data["link"]
        SubElement(item, "description").text = item_data["description"]
        SubElement(item, "guid", isPermaLink="false").text = item_data["guid"]
        if item_data["pubDate"]:
            SubElement(item, "pubDate").text = item_data["pubDate"]

    xml_decl = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml_body = tostring(rss, encoding="unicode")
    existing_feed_file.write_text(xml_decl + xml_body)
    return len(existing_items)


def clone_feed_repo(repo_dir=None):
    if repo_dir and repo_dir.exists() and (repo_dir / ".git").exists():
        subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only"],
                       capture_output=True, text=True)
        return repo_dir

    if repo_dir:
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", FEED_REPO, str(repo_dir)],
                       capture_output=True, text=True)
        return repo_dir

    tmp = Path(tempfile.mkdtemp(prefix="yt-feed-"))
    subprocess.run(["git", "clone", FEED_REPO, str(tmp)],
                   capture_output=True, text=True)
    return tmp


def publish_feed(repo_dir):
    subprocess.run(["git", "-C", str(repo_dir), "add", "feed.xml"],
                   capture_output=True, text=True)
    diff = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if diff.returncode == 0:
        print("Feed unchanged — nothing to push")
        return

    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "Update comment feed"],
        capture_output=True, text=True,
    )
    push = subprocess.run(
        ["git", "-C", str(repo_dir), "push"],
        capture_output=True, text=True,
    )
    if push.returncode == 0:
        print(f"Feed published to {FEED_URL}")
    else:
        print(f"Failed to push feed: {push.stderr}")


def main():
    args = parse_args()

    repo_dir = clone_feed_repo(args.feed_repo_dir)
    feed_file = repo_dir / "feed.xml"

    existing_guids = load_existing_guids(feed_file)
    print(f"Existing feed has {len(existing_guids)} items")

    videos = discover_videos(args.tier)

    all_comments = []
    new_count = 0

    with tempfile.TemporaryDirectory(prefix="yt-scrape-") as work_dir:
        work_path = Path(work_dir)
        for i, video in enumerate(videos):
            vid = video["id"]
            title = video["title"]
            print(f"[{i+1}/{len(videos)}] {title} ({vid})")

            comments = fetch_comments(vid, work_path)
            new_in_video = sum(1 for c in comments if c["comment_id"] not in existing_guids)
            all_comments.extend(comments)
            new_count += new_in_video
            print(f"  {len(comments)} comments ({new_in_video} new)")

            if i < len(videos) - 1:
                time.sleep(DELAY_BETWEEN_VIDEOS)

    print(f"\nTotal: {len(all_comments)} comments scraped, {new_count} new")

    if new_count > 0:
        total_items = build_feed(feed_file, all_comments)
        print(f"Feed updated: {total_items} items")
        publish_feed(repo_dir)
    else:
        print("No new comments — feed not updated")


if __name__ == "__main__":
    main()
