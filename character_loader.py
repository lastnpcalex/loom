"""Parse character .md files into structured data."""

import os
import re
from pathlib import Path
from typing import Optional


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML-like frontmatter and body from markdown."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', text, re.DOTALL)
    if not match:
        return {}, text

    meta = {}
    for line in match.group(1).strip().splitlines():
        if ':' in line:
            key, val = line.split(':', 1)
            key = key.strip()
            val = val.strip()
            # Parse list values like [tag1, tag2]
            if val.startswith('[') and val.endswith(']'):
                val = [v.strip().strip('"\'') for v in val[1:-1].split(',')]
            meta[key] = val
    return meta, match.group(2)


def parse_sections(body: str) -> dict[str, str]:
    """Split markdown body into sections by # headers."""
    sections = {}
    current_key = None
    current_lines = []

    for line in body.splitlines():
        if line.startswith('# ') and not line.startswith('## '):
            if current_key:
                sections[current_key] = '\n'.join(current_lines).strip()
            current_key = line[2:].strip().lower()
            current_lines = []
        else:
            current_lines.append(line)

    if current_key:
        sections[current_key] = '\n'.join(current_lines).strip()
    return sections


def parse_example_messages(text: str) -> list[dict]:
    """Parse example message blocks into role/content pairs."""
    messages = []
    if not text:
        return messages

    # Split by ## Example headers
    blocks = re.split(r'## Example \d+\s*\n', text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        for line_block in re.split(r'\n(?=(?:user|assistant):)', block):
            line_block = line_block.strip()
            if line_block.startswith('user:'):
                messages.append({"role": "user", "content": line_block[5:].strip()})
            elif line_block.startswith('assistant:'):
                messages.append({"role": "assistant", "content": line_block[10:].strip()})
    return messages


def load_character(filepath: str) -> Optional[dict]:
    """Load a single character from a .md file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
    except (IOError, UnicodeDecodeError):
        return None

    meta, body = parse_frontmatter(text)
    sections = parse_sections(body)

    char = {
        "id": Path(filepath).stem,
        "name": meta.get("name", Path(filepath).stem),
        "avatar": meta.get("avatar", None),
        "tags": meta.get("tags", []),
        "personality": sections.get("personality", ""),
        "scenario": sections.get("scenario", ""),
        "greeting": sections.get("greeting", ""),
        "example_messages": parse_example_messages(sections.get("example messages", "")),
        "filepath": filepath,
    }
    return char


def load_all_characters(directory: str = "characters") -> list[dict]:
    """Load all character .md files from a directory."""
    characters = []
    if not os.path.isdir(directory):
        return characters

    for fname in sorted(os.listdir(directory)):
        if fname.endswith('.md'):
            char = load_character(os.path.join(directory, fname))
            if char:
                characters.append(char)
    return characters


def slugify(name: str) -> str:
    """Convert a name to a filename-safe slug."""
    slug = re.sub(r'[^\w\s-]', '', name.lower().strip())
    slug = re.sub(r'[\s_]+', '-', slug)
    return slug or 'unnamed'


def save_character(directory: str, data: dict) -> dict:
    """Save a character as a .md file and return the loaded character dict.

    data keys: name, tags (list or comma-string), personality, scenario,
               greeting, example_messages_raw (optional raw text block)
    """
    os.makedirs(directory, exist_ok=True)

    char_id = data.get("id") or slugify(data["name"])
    name = data["name"]
    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    # Build frontmatter
    tags_str = "[" + ", ".join(tags) + "]" if tags else "[]"
    lines = [
        "---",
        f"name: {name}",
        "avatar: null",
        f"tags: {tags_str}",
        "---",
    ]

    # Sections
    personality = data.get("personality", "").strip()
    scenario = data.get("scenario", "").strip()
    greeting = data.get("greeting", "").strip()
    examples_raw = data.get("example_messages_raw", "").strip()

    if personality:
        lines.append(f"\n# Personality\n{personality}")
    if scenario:
        lines.append(f"\n# Scenario\n{scenario}")
    if greeting:
        lines.append(f"\n# Greeting\n{greeting}")
    if examples_raw:
        lines.append(f"\n# Example Messages\n{examples_raw}")

    filepath = os.path.join(directory, f"{char_id}.md")
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines) + "\n")

    return load_character(filepath)


def delete_character(directory: str, char_id: str) -> bool:
    """Delete a character .md file. Returns True if deleted."""
    filepath = os.path.join(directory, f"{char_id}.md")
    if os.path.exists(filepath):
        os.remove(filepath)
        return True
    return False


# ── Persona Files ──

def load_persona(filepath: str) -> Optional[dict]:
    """Load a user persona from a .md file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
    except (IOError, UnicodeDecodeError):
        return None

    meta, body = parse_frontmatter(text)
    return {
        "id": Path(filepath).stem,
        "name": meta.get("name", Path(filepath).stem),
        "tags": meta.get("tags", []),
        "content": body.strip(),
        "filepath": filepath,
    }


def load_all_personas(directory: str = "personas") -> list[dict]:
    """Load all persona .md files from a directory."""
    personas = []
    if not os.path.isdir(directory):
        return personas
    for fname in sorted(os.listdir(directory)):
        if fname.endswith('.md'):
            persona = load_persona(os.path.join(directory, fname))
            if persona:
                personas.append(persona)
    return personas


# ── Lore/History Files ──

def load_lore_entry(filepath: str) -> Optional[dict]:
    """Load a lore/history file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
    except (IOError, UnicodeDecodeError):
        return None

    meta, body = parse_frontmatter(text)
    return {
        "id": Path(filepath).stem,
        "name": meta.get("name", Path(filepath).stem),
        "tags": meta.get("tags", []),
        "content": body.strip(),
        "filepath": filepath,
    }


def load_all_lore(directory: str = "lore") -> list[dict]:
    """Load all lore .md files from a directory."""
    entries = []
    if not os.path.isdir(directory):
        return entries
    for fname in sorted(os.listdir(directory)):
        if fname.endswith('.md'):
            entry = load_lore_entry(os.path.join(directory, fname))
            if entry:
                entries.append(entry)
    return entries
