"""Deterministic mock taste graph (fictional demo data).

Fixtures mirror the shape of public Qloo documentation examples: entities
across domains plus cross-domain affinity edges. The mock graph exists so
Judge Mode runs the full agent loop with zero API key; it never pretends
to be real Qloo data — every mock response is labelled source="mock".
"""
from __future__ import annotations

from .models import Entity

E = Entity
ENTITIES: list[Entity] = [
    # film
    E("mov_spirited_away", "Spirited Away", "film", ("animation", "fantasy", "japanese", "whimsical", "hand-drawn")),
    E("mov_blade_runner", "Blade Runner", "film", ("sci-fi", "noir", "dystopian", "atmospheric")),
    E("mov_amelie", "Amelie", "film", ("romance", "french", "whimsical", "warm")),
    E("mov_parasite", "Parasite", "film", ("thriller", "korean", "social", "dark")),
    E("mov_midnight_paris", "Midnight in Paris", "film", ("romance", "paris", "nostalgic", "literary")),
    E("mov_lost_translation", "Lost in Translation", "film", ("drama", "tokyo", "quiet", "atmospheric")),
    E("mov_grand_budapest", "The Grand Budapest Hotel", "film", ("comedy", "whimsical", "symmetrical", "warm")),
    # music
    E("art_kind_of_blue", "Kind of Blue (Miles Davis)", "music", ("jazz", "modal", "quiet", "classic")),
    E("art_debussy", "Claude Debussy", "music", ("classical", "impressionist", "dreamy", "quiet")),
    E("art_radiohead", "Radiohead", "music", ("alternative", "atmospheric", "experimental")),
    E("art_nujabes", "Nujabes", "music", ("jazz", "hip-hop", "japanese", "mellow", "lo-fi")),
    E("art_bon_iver", "Bon Iver", "music", ("folk", "quiet", "winter", "intimate")),
    E("art_fleetwood", "Fleetwood Mac", "music", ("classic-rock", "warm", "nostalgic")),
    # books
    E("bok_norwegian_wood", "Norwegian Wood", "book", ("murakami", "japanese", "melancholy", "literary")),
    E("bok_wind_up_bird", "The Wind-Up Bird Chronicle", "book", ("murakami", "japanese", "surreal", "literary")),
    E("bok_little_prince", "The Little Prince", "book", ("french", "whimsical", "philosophical", "short")),
    E("bok_neuromancer", "Neuromancer", "book", ("sci-fi", "cyberpunk", "noir", "dystopian")),
    E("bok_klara", "Klara and the Sun", "book", ("sci-fi", "gentle", "literary")),
    # places (Kyoto / NYC / Paris flavours)
    E("plc_gion", "Gion, Kyoto", "place", ("kyoto", "historic", "quiet", "japanese"), "Kyoto"),
    E("plc_arashiyama", "Arashiyama Bamboo Grove", "place", ("kyoto", "nature", "quiet", "japanese"), "Kyoto"),
    E("plc_fushimi", "Fushimi Inari Shrine", "place", ("kyoto", "historic", "japanese", "walking"), "Kyoto"),
    E("plc_west_village", "West Village, New York", "place", ("nyc", "walking", "jazz", "historic"), "New York"),
    E("plc_montmartre", "Montmartre, Paris", "place", ("paris", "art", "walking", "french"), "Paris"),
    E("plc_kyoto_museum", "Kyoto National Museum", "place", ("kyoto", "art", "historic", "quiet"), "Kyoto"),
    # food (places with a restaurant flavour tag)
    E("fod_ramen_kyoto", "A quiet ramen counter in Gion", "food", ("kyoto", "ramen", "japanese", "intimate"), "Kyoto"),
    E("fod_kaiseki", "A seasonal kaiseki tasting menu", "food", ("kyoto", "japanese", "seasonal", "quiet"), "Kyoto"),
    E("fod_jazz_bar_nyc", "A basement jazz bar with natural wine", "food", ("nyc", "jazz", "intimate", "wine"), "New York"),
    E("fod_paris_bistro", "A corner bistro in Montmartre", "food", ("paris", "french", "warm", "classic"), "Paris"),
    E("fod_izakaya", "A lantern-lit izakaya in Pontocho Alley", "food", ("kyoto", "japanese", "warm", "walking"), "Kyoto"),
    # brands (gift-able)
    E("brd_muji", "Muji", "brand", ("japanese", "minimal", "quiet", "design")),
    E("brd_aesop", "Aesop", "brand", ("minimal", "botanical", "design", "quiet")),
    E("brd_leica", "Leica", "brand", ("photography", "classic", "craft", "design")),
    E("brd_studio_ghibli", "Studio Ghibli Museum Shop", "brand", ("japanese", "animation", "whimsical", "gift")),
    # destinations
    E("dst_kyoto", "Kyoto", "destination", ("japanese", "historic", "quiet", "walking"), "Kyoto"),
    E("dst_paris", "Paris", "destination", ("french", "art", "romance", "walking"), "Paris"),
]

BY_ID: dict[str, Entity] = {e.id: e for e in ENTITIES}
BY_NAME: dict[str, Entity] = {e.name.lower(): e for e in ENTITIES}

# Cross-domain affinity edges: seed_id -> [(target_id, affinity)].
# This is the mock stand-in for Qloo's taste-graph correlations.
EDGES: dict[str, list[tuple[str, float]]] = {
    "mov_spirited_away": [("art_nujabes", 0.91), ("bok_wind_up_bird", 0.84), ("plc_gion", 0.88),
                          ("fod_ramen_kyoto", 0.86), ("brd_studio_ghibli", 0.95), ("dst_kyoto", 0.9),
                          ("plc_arashiyama", 0.82), ("fod_izakaya", 0.8),
                          # Lower-affinity third picks: the plan shows the
                          # top two per domain, these surface as verified
                          # alternatives (see agent.py).
                          ("plc_kyoto_museum", 0.75), ("fod_kaiseki", 0.72)],
    "mov_blade_runner": [("art_radiohead", 0.9), ("bok_neuromancer", 0.94), ("brd_leica", 0.72),
                         ("mov_lost_translation", 0.78), ("plc_west_village", 0.66)],
    "mov_amelie": [("plc_montmartre", 0.93), ("fod_paris_bistro", 0.89), ("bok_little_prince", 0.87),
                   ("dst_paris", 0.92), ("mov_midnight_paris", 0.85), ("brd_aesop", 0.7)],
    "mov_parasite": [("art_radiohead", 0.76), ("bok_klara", 0.7), ("mov_blade_runner", 0.74)],
    "mov_lost_translation": [("dst_kyoto", 0.81), ("art_nujabes", 0.85), ("plc_gion", 0.79),
                             ("bok_norwegian_wood", 0.83)],
    "mov_grand_budapest": [("brd_aesop", 0.74), ("art_debussy", 0.72), ("plc_montmartre", 0.75)],
    "art_kind_of_blue": [("fod_jazz_bar_nyc", 0.92), ("plc_west_village", 0.88), ("art_nujabes", 0.84),
                         ("bok_norwegian_wood", 0.71), ("mov_lost_translation", 0.77)],
    "art_debussy": [("plc_arashiyama", 0.8), ("bok_little_prince", 0.78), ("mov_amelie", 0.82)],
    "art_radiohead": [("mov_blade_runner", 0.9), ("bok_neuromancer", 0.83), ("mov_parasite", 0.76)],
    "art_nujabes": [("mov_spirited_away", 0.91), ("plc_gion", 0.84), ("fod_ramen_kyoto", 0.83),
                    ("bok_norwegian_wood", 0.8), ("brd_muji", 0.73)],
    "art_bon_iver": [("plc_arashiyama", 0.79), ("bok_klara", 0.74), ("brd_muji", 0.7)],
    "art_fleetwood": [("fod_paris_bistro", 0.68), ("mov_midnight_paris", 0.72), ("brd_leica", 0.66)],
    "bok_norwegian_wood": [("art_kind_of_blue", 0.71), ("art_nujabes", 0.8), ("plc_gion", 0.82),
                           ("fod_ramen_kyoto", 0.78), ("dst_kyoto", 0.85), ("mov_lost_translation", 0.83)],
    "bok_wind_up_bird": [("mov_spirited_away", 0.84), ("art_nujabes", 0.79), ("plc_fushimi", 0.76),
                         ("brd_muji", 0.72)],
    "bok_neuromancer": [("mov_blade_runner", 0.94), ("art_radiohead", 0.83), ("brd_leica", 0.7)],
    "bok_little_prince": [("mov_amelie", 0.87), ("art_debussy", 0.78), ("dst_paris", 0.8)],
}


def shared_tags(a: Entity, b: Entity) -> tuple[str, ...]:
    return tuple(sorted(set(a.tags) & set(b.tags)))
