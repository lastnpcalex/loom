#!/usr/bin/env python3
"""Bluesky public API CLI — no login required.

Wraps the public ATProto XRPC endpoints so CC skills can just call:
    python bsky_cli.py feed lastnpcalex.agency
    python bsky_cli.py profile bsky.app
    python bsky_cli.py post at://did:plc:abc/app.bsky.feed.post/xyz
    python bsky_cli.py thread at://did:plc:abc/app.bsky.feed.post/xyz
    python bsky_cli.py resolve bsky.app
    python bsky_cli.py search "atproto developers" 15
"""

import io
import json
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE = "https://public.api.bsky.app/xrpc"


def _get(endpoint: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"{BASE}/{endpoint}?{qs}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "bsky-cli/1.0 (Loom; +https://github.com/lastnpcalex/a-shadow-loom)",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
            msg = err.get("message", body)
        except json.JSONDecodeError:
            msg = body[:200]
        if e.code == 403:
            print(f"Error 403: Access denied for {endpoint}. This endpoint may require authentication.", file=sys.stderr)
        else:
            print(f"Error {e.code}: {msg}", file=sys.stderr)
        sys.exit(1)


def _ts(iso: str) -> str:
    """Format ISO timestamp to readable short form."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d %H:%M")
    except Exception:
        return iso[:16]


# ── Commands ──────────────────────────────────────────────────────────────

def cmd_feed(actor: str, limit: int = 25):
    data = _get("app.bsky.feed.getAuthorFeed", {"actor": actor, "limit": min(limit, 100)})
    posts = data.get("feed", [])
    if not posts:
        print(f"No posts found for {actor}")
        return
    print(f"Recent posts from {actor} ({len(posts)} shown):\n")
    for i, item in enumerate(posts, 1):
        # Skip reposts
        if item.get("reason", {}).get("$type", "") == "app.bsky.feed.defs#reasonRepost":
            continue
        p = item["post"]
        rec = p.get("record", {})
        text = rec.get("text", "").replace("\n", " ")[:200]
        ts = _ts(rec.get("createdAt", ""))
        likes = p.get("likeCount", 0)
        reposts = p.get("repostCount", 0)
        replies = p.get("replyCount", 0)
        uri = p.get("uri", "")
        print(f"{i}. [{ts}] {text}")
        print(f"   ♥ {likes}  ♻ {reposts}  💬 {replies}")
        print(f"   {uri}")
        print()


def cmd_profile(actor: str):
    data = _get("app.bsky.actor.getProfile", {"actor": actor})
    name = data.get("displayName", "(none)")
    handle = data.get("handle", "?")
    did = data.get("did", "?")
    bio = data.get("description", "").replace("\n", " ")[:300]
    followers = data.get("followersCount", 0)
    following = data.get("followsCount", 0)
    posts = data.get("postsCount", 0)
    labels = [l.get("val", "") for l in data.get("labels", [])]

    print(f"Profile: {name} (@{handle})")
    print(f"DID: {did}")
    if bio:
        print(f"Bio: {bio}")
    print(f"Posts: {posts}  Followers: {followers}  Following: {following}")
    if labels:
        print(f"Labels: {', '.join(labels)}")


def cmd_post(at_uri: str):
    data = _get("app.bsky.feed.getPosts", {"uris": at_uri})
    posts = data.get("posts", [])
    if not posts:
        print(f"Post not found: {at_uri}")
        return
    p = posts[0]
    author = p.get("author", {})
    rec = p.get("record", {})
    print(f"@{author.get('handle', '?')} ({author.get('displayName', '')})")
    print(f"  {_ts(rec.get('createdAt', ''))}")
    print()
    print(rec.get("text", "(no text)"))
    print()
    print(f"♥ {p.get('likeCount', 0)}  ♻ {p.get('repostCount', 0)}  💬 {p.get('replyCount', 0)}")
    print(f"URI: {p.get('uri', '')}")

    # Embeds
    embed = p.get("embed", {})
    etype = embed.get("$type", "")
    if "images" in etype:
        for img in embed.get("images", []):
            print(f"  [Image] {img.get('alt', '(no alt)')}: {img.get('fullsize', '')}")
    if "external" in etype:
        ext = embed.get("external", {})
        print(f"  [Link] {ext.get('title', '')}: {ext.get('uri', '')}")
    if "record" in etype:
        qr = embed.get("record", {})
        if isinstance(qr, dict) and qr.get("uri"):
            print(f"  [Quote] {qr.get('uri', '')}")


def cmd_thread(at_uri: str, depth: int = 6):
    data = _get("app.bsky.feed.getPostThread", {"uri": at_uri, "depth": min(depth, 20)})
    thread = data.get("thread", {})
    if not thread or thread.get("$type") == "app.bsky.feed.defs#notFoundPost":
        print(f"Thread not found: {at_uri}")
        return

    def print_node(node, indent=0):
        if node.get("$type") == "app.bsky.feed.defs#blockedPost":
            print(f"{'  ' * indent}[blocked]")
            return
        p = node.get("post", {})
        if not p:
            return
        author = p.get("author", {})
        rec = p.get("record", {})
        text = rec.get("text", "").replace("\n", " ")[:200]
        handle = author.get("handle", "?")
        ts = _ts(rec.get("createdAt", ""))
        likes = p.get("likeCount", 0)
        prefix = "  " * indent
        print(f"{prefix}@{handle} [{ts}] (♥ {likes})")
        print(f"{prefix}  {text}")
        print()
        for reply in node.get("replies", []):
            print_node(reply, indent + 1)

    # Print parent chain if available
    parent = thread.get("parent")
    if parent and parent.get("post"):
        print("─── Parent ───")
        print_node(parent)
        print("─── Thread ───")

    print_node(thread)


def cmd_resolve(handle: str):
    data = _get("com.atproto.identity.resolveHandle", {"handle": handle})
    did = data.get("did", "?")
    print(f"{handle} → {did}")
    print(f"\nUse this DID with other /bsky-* commands as the actor parameter.")


def cmd_search(query: str, limit: int = 20):
    data = _get("app.bsky.feed.searchPosts", {"q": query, "limit": min(limit, 100)})
    posts = data.get("posts", [])
    total = data.get("hitsTotal", len(posts))
    if not posts:
        print(f"No results for '{query}'. Try different keywords.")
        return
    print(f"Search: '{query}' — {total} total hits, showing {len(posts)}:\n")
    for i, p in enumerate(posts, 1):
        author = p.get("author", {})
        rec = p.get("record", {})
        text = rec.get("text", "").replace("\n", " ")[:200]
        ts = _ts(rec.get("createdAt", ""))
        likes = p.get("likeCount", 0)
        uri = p.get("uri", "")
        print(f"{i}. @{author.get('handle', '?')} [{ts}] (♥ {likes})")
        print(f"   {text}")
        print(f"   {uri}")
        print()


# ── CLI dispatch ──────────────────────────────────────────────────────────

COMMANDS = {
    "feed": (cmd_feed, "feed <handle> [limit]"),
    "profile": (cmd_profile, "profile <handle-or-DID>"),
    "post": (cmd_post, "post <at-uri>"),
    "thread": (cmd_thread, "thread <at-uri> [depth]"),
    "resolve": (cmd_resolve, "resolve <handle>"),
    "search": (cmd_search, 'search "<query>" [limit]'),
}


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in COMMANDS:
        print("Usage: python bsky_cli.py <command> <args>")
        print("\nCommands:")
        for name, (_, usage) in COMMANDS.items():
            print(f"  {usage}")
        sys.exit(1)

    cmd_name = sys.argv[1]
    func, _ = COMMANDS[cmd_name]
    args = sys.argv[2:]

    if cmd_name in ("feed", "search", "thread"):
        # First arg is required, second is optional int
        main_arg = args[0]
        extra = int(args[1]) if len(args) > 1 else None
        if extra is not None:
            func(main_arg, extra)
        else:
            func(main_arg)
    else:
        func(args[0])


if __name__ == "__main__":
    main()
