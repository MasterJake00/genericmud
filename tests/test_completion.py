"""Autocomplete from recent output: the word index and the cycling state."""

from __future__ import annotations

from genericmud.completion import CompletionCycler, OutputWordIndex


def test_words_come_back_newest_first():
    index = OutputWordIndex()
    index.add_line("a goblin arrives")
    index.add_line("a gnome arrives")
    assert index.complete("g") == ["gnome", "goblin"]


def test_reseen_word_moves_to_the_front_without_duplicating():
    index = OutputWordIndex()
    index.add_line("goblin")
    index.add_line("gnome")
    index.add_line("the goblin snarls")
    assert index.complete("g") == ["goblin", "gnome"]


def test_prefix_match_is_case_insensitive_and_skips_the_exact_word():
    index = OutputWordIndex()
    index.add_line("Vaelin arrives from the north")
    assert index.complete("vae") == ["Vaelin"]
    assert index.complete("Vaelin") == []  # already fully typed; nothing to add


def test_short_words_are_not_indexed():
    index = OutputWordIndex()
    index.add_line("go up to it")
    assert index.complete("g") == []  # "go"/"up"/"to"/"it" all under the length floor


def test_index_is_capped():
    index = OutputWordIndex(max_words=3)
    index.add_line("alpha bravo charlie delta")
    assert len(index.complete("")) == 0  # empty prefix never completes
    assert index.complete("a") == []  # "alpha" (oldest of the four) aged out
    assert index.complete("d") == ["delta"]


def test_cycler_wraps_forward_and_backward():
    cycler = CompletionCycler()
    cycler.begin("g", ["gnome", "goblin"])
    assert cycler.next() == "gnome"
    assert cycler.next() == "goblin"
    assert cycler.next() == "gnome"  # wraps
    assert cycler.prev() == "goblin"  # steps back


def test_cycler_can_start_in_reverse():
    cycler = CompletionCycler()
    cycler.begin("g", ["gnome", "goblin", "goblin-king"])
    assert cycler.prev() == "goblin-king"
    assert cycler.prev() == "goblin"


def test_cycler_reset_ends_the_run():
    cycler = CompletionCycler()
    cycler.begin("g", ["gnome"])
    assert cycler.active
    cycler.reset()
    assert not cycler.active
    assert cycler.next() is None


def test_words_in_any_alphabet_complete_whole():
    # Only ASCII letters counted as a word, so a Russian MUD's output (readable now that a
    # world can be set to KOI8-R) completed nothing, and an accented name was cut short:
    # "Café" went into the index as "Caf", and Ctrl+Space typed a word the MUD never said.
    index = OutputWordIndex()
    index.add_line("Вы видите стражника и Алебарду у двери.")
    index.add_line("Monsieur Müller's Café is open.")
    assert index.complete("стр") == ["стражника"]
    assert index.complete("але") == ["Алебарду"]
    assert index.complete("caf") == ["Café"]
    assert index.complete("mül") == ["Müller's"]
    assert index.complete("12") == []  # digits still don't start a word


def test_a_word_seen_again_keeps_its_newest_spelling_and_place():
    index = OutputWordIndex()
    index.add_line("goblin gnome GOBLIN")  # later in a line is newer
    assert index.complete("g") == ["GOBLIN", "gnome"]


def test_the_index_matches_the_plain_list_it_replaced():
    # The index used to be a newest-first list, rebuilt for every word: obviously right, too
    # slow for every line a MUD sends. Whatever the lines, the faster one must agree with it.
    import random
    import re

    def reference(lines, max_words):
        words: list[str] = []
        for line in lines:
            found = [w for w in re.findall(r"[^\W\d_](?:[^\W\d_]|['-])+", line) if len(w) >= 3]
            for word in found:
                words = [w for w in words if w.lower() != word.lower()]
                words.insert(0, word)
            del words[max_words:]
        return words

    rng = random.Random(7)
    vocabulary = ["goblin", "Goblin", "gnome", "GNOME", "golem", "gold", "Gold", "grate",
                  "drake", "Drake", "dragon", "door", "doors", "Dorn", "glimmer", "gloom"]
    lines = [" ".join(rng.choice(vocabulary) for _ in range(rng.randint(0, 9)))
             for _ in range(300)]
    index = OutputWordIndex(max_words=7)
    for line in lines:
        index.add_line(line)
    expected = reference(lines, 7)
    for prefix in ("g", "go", "gol", "d", "dr", "do", "Gl", "x"):
        needle = prefix.lower()
        assert index.complete(prefix) == [
            w for w in expected if w.lower().startswith(needle) and w.lower() != needle
        ]
