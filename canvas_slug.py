"""Human-readable slug generator for canvas direct-access URLs.

Slug shape: `<adjective>-<noun>-<4-char-suffix>`, e.g. `dark-penguin-x7k2`.
Suffix uses Crockford-style lowercase base32 (no `l`, no `1`, no `o`, no `0`)
so slugs read unambiguously and survive screenshots.

The pattern is strict enough that the resolver route can validate it via regex
and avoid shadowing other top-level paths.
"""

import re
import secrets

ADJECTIVES = [
    "amber", "ancient", "autumn", "blue", "bold", "brave", "bright", "brisk",
    "calm", "clever", "cosmic", "crimson", "crisp", "curious", "dapper", "dark",
    "deep", "dim", "distant", "drowsy", "dusky", "eager", "earnest", "echoing",
    "elder", "electric", "emerald", "empty", "endless", "fading", "faint", "fair",
    "fallow", "feral", "fierce", "first", "flickering", "floating", "frosty", "gentle",
    "glassy", "gleaming", "glowing", "golden", "graceful", "grave", "hazy", "hidden",
    "hollow", "humble", "icy", "idle", "immense", "iron", "ivory", "jagged",
    "jolly", "kind", "lambent", "lazy", "lilac", "lively", "lonely", "lucent",
    "lucid", "lunar", "mauve", "mellow", "merry", "midnight", "milky", "misty",
    "morning", "mossy", "muted", "narrow", "nimble", "noble", "northern", "obscure",
    "ochre", "olive", "patient", "pearly", "polar", "quiet", "radiant", "rapid",
    "raven", "restless", "rosy", "rough", "rugged", "rustic", "sable", "sage",
    "saffron", "scarlet", "secret", "shaded", "silent", "silken", "silver", "sleepy",
    "slender", "smoky", "solar", "solemn", "somber", "southern", "sparse", "spry",
    "stark", "steely", "still", "stoic", "stormy", "sudden", "sunken", "swift",
    "tame", "tangled", "tender", "thawing", "tidal", "timid", "tranquil", "twilight",
    "umber", "valiant", "velvet", "verdant", "violet", "vivid", "waking", "wandering",
    "warm", "weary", "western", "whispering", "wild", "windswept", "wintry", "wistful",
    "wry", "yawning", "young", "zealous",
]

NOUNS = [
    "alder", "anchor", "antler", "apple", "arrow", "ash", "aspen", "badger",
    "balcony", "basin", "beacon", "bear", "beech", "beetle", "birch", "bison",
    "boulder", "brambles", "brook", "buffalo", "cairn", "canyon", "cardinal", "cedar",
    "chapel", "cinder", "cliff", "clover", "cobra", "comet", "compass", "coral",
    "cormorant", "cottage", "crane", "crescent", "crow", "crystal", "cypress", "dawn",
    "deer", "delta", "den", "dolphin", "drift", "dune", "dusk", "eagle",
    "ember", "estuary", "falcon", "feather", "fennec", "fern", "ferry", "field",
    "finch", "fjord", "flame", "fog", "forest", "fox", "garden", "geode",
    "ghost", "glacier", "glade", "globe", "goose", "gorge", "granite", "grove",
    "harbor", "hare", "harvest", "hawk", "hazel", "headland", "hedge", "heron",
    "hollow", "horizon", "hound", "ibis", "iris", "island", "ivory", "jay",
    "kestrel", "knoll", "lagoon", "lantern", "lark", "ledge", "leopard", "lichen",
    "linnet", "lupine", "lynx", "magpie", "manor", "maple", "marsh", "meadow",
    "mesa", "mink", "mistral", "moss", "moth", "mountain", "moth", "newt",
    "oak", "ocean", "orchard", "osprey", "otter", "owl", "panther", "path",
    "pebble", "pelican", "penguin", "pine", "pond", "poplar", "prairie", "puffin",
    "quartz", "quail", "rabbit", "raven", "reef", "ridge", "river", "robin",
    "rook", "salmon", "sand", "saplng", "seal", "sequoia", "shadow", "shell",
    "shore", "silo", "skylark", "slope", "snow", "sparrow", "spire", "spruce",
    "starling", "stoat", "stone", "stream", "summit", "swallow", "swan", "tamarisk",
    "thicket", "thistle", "thrush", "tide", "torch", "tower", "trail", "tundra",
    "valley", "vesper", "vine", "violet", "vulture", "warbler", "waterfall", "wave",
    "willow", "wolf", "woodland", "wren",
]

_SUFFIX_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # 31 chars, ambiguity-free

SLUG_RE = re.compile(r"^[a-z]+-[a-z]+-[a-z0-9]{4}$")


def generate_canvas_slug() -> str:
    """Return a fresh random slug. Uniqueness must be enforced at the DB layer."""
    adj = secrets.choice(ADJECTIVES)
    noun = secrets.choice(NOUNS)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(4))
    return f"{adj}-{noun}-{suffix}"


def is_valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.match(slug or ""))
