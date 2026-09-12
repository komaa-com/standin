# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The mouth, and the face it sits in.

A realtime model returns no phoneme timings, so without an estimate the avatar's
mouth never moves at all. The estimate is only worth sending because it is
spread over audio the worker MEASURED, which is why most of what is pinned here
is timing: a division by zero, a step under half a millisecond, milliseconds a
cancelled turn left behind. The rest is coverage of the table itself, because a
letter missing from it is a syllable the mouth stalls on, and an Arabic reply
with no rows at all is a mouth that never moves for half the callers.

The emotion inferrer ships in ``standin.avatar``, next to ``expression()``,
because what it produces is that function's argument. It is tested here with the
lip-sync half it was specified alongside.
"""

from __future__ import annotations

import json

import pytest

from standin.avatar import (
    EMOTIONS,
    MAX_VISEME_ID,
    ExpressionCue,
    infer_emotion,
    speech_marks,
)
from standin.lipsync import (
    CHAR_VISEMES,
    SILENCE_VISEME,
    TurnLipSync,
    estimate_visemes,
    viseme_for_char,
    visemes_from_alignment,
)

pytestmark = pytest.mark.unit

#: One second of PCM16 mono at the wire's 16 kHz.
ONE_SECOND = b"\x00\x00" * 16_000

LATIN = "abcdefghijklmnopqrstuvwxyz"
ARABIC = "ابتثجحخدذرزسشصضطظعغفقكلمنهوي"


# ------------------------------------------------------------------ the table


def test_every_latin_letter_has_a_mouth_shape():
    """One unmapped common letter thins the timeline unevenly, and the mouth
    stalls on whichever syllable holds it."""
    assert [ch for ch in LATIN if viseme_for_char(ch) is None] == []


def test_every_arabic_letter_has_a_mouth_shape():
    """The bilingual failure this table exists to prevent: with no Arabic rows
    an Arabic reply produces no tokens and carries no timeline at all."""
    assert [ch for ch in ARABIC if viseme_for_char(ch) is None] == []


def test_the_arabic_variant_forms_are_mapped_too():
    for ch in "أإآىةئءؤ":
        assert viseme_for_char(ch) is not None


def test_the_short_vowels_are_mapped_and_the_other_marks_are_not():
    """Fatha, damma and kasra are the truest mouth shapes a voweled text has.
    Sukun, shadda and tanween carry no shape of their own, so mapping them would
    insert mouth changes nobody spoke."""
    assert (viseme_for_char("َ"), viseme_for_char("ُ"), viseme_for_char("ِ")) == (
        2,
        7,
        6,
    )
    for mark in "ًٌٍّْ":
        assert viseme_for_char(mark) is None


def test_the_tatweel_is_a_stretch_and_not_a_sound():
    assert viseme_for_char("ـ") is None


def test_a_capital_letter_wears_the_same_shape_as_its_lowercase():
    assert viseme_for_char("M") == viseme_for_char("m")


def test_a_digit_and_a_punctuation_mark_have_no_shape():
    assert viseme_for_char("3") is None
    assert viseme_for_char("%") is None


def test_every_shape_in_the_table_is_inside_the_viseme_range():
    assert all(0 <= viseme <= MAX_VISEME_ID for viseme in CHAR_VISEMES.values())


def test_the_table_cannot_be_edited_at_runtime():
    """Two SDKs agree on this map byte for byte. A caller that could mutate it
    would make one call's mouth disagree with every other call's."""
    with pytest.raises(TypeError):
        CHAR_VISEMES["a"] = 9  # type: ignore[index]


def test_silence_is_token_zero():
    assert SILENCE_VISEME == 0


# ---------------------------------------------------------------- estimating


def test_a_sentence_is_spread_over_the_audio_it_was_spoken_in():
    assert estimate_visemes("hello", 1000) == [(0, 12), (200, 4), (400, 14), (800, 8)]


def test_the_space_between_two_words_closes_the_mouth():
    assert estimate_visemes("a b", 100) == [(0, 2), (33, SILENCE_VISEME), (67, 21)]


def test_an_astral_character_is_skipped_whole():
    """Walking by code unit would split the surrogate pair and look up two
    broken halves, putting garbage between the two real letters."""
    assert estimate_visemes("a\U0001f600b", 100) == [(0, 2), (50, 21)]


def test_nothing_is_returned_for_empty_text():
    assert estimate_visemes("", 1000) == []
    assert estimate_visemes("   \n\t ", 1000) == []
    assert estimate_visemes(None, 1000) == []


def test_nothing_is_returned_for_a_duration_that_is_not_positive():
    """Dividing by it produces infinite timestamps, and a mouth that is
    desynchronised for the rest of the utterance."""
    assert estimate_visemes("hello", 0) == []
    assert estimate_visemes("hello", -250) == []


def test_a_duration_that_is_not_a_finite_number_produces_no_marks():
    """Nothing in the lip-sync path may raise into the call, and a duration
    arrives from arithmetic a plugin did: a rate of zero, a division that went
    to infinity. The other SDK answers with no marks, so this one does too."""
    assert estimate_visemes("hello", float("nan")) == []
    assert estimate_visemes("hello", float("inf")) == []


def test_text_with_no_mouth_shape_in_it_returns_nothing():
    """'3.5%' is all unmapped characters. Mapping them to silence instead would
    punch a hole of closed-mouth frames into a spoken number."""
    assert estimate_visemes("3.5% ... ?", 500) == []


def test_a_run_of_one_shape_is_one_mark():
    """'mmm' is one mouth position, not three, and a mark per character
    multiplies the payload for an identical rendering."""
    assert estimate_visemes("mmm", 300) == [(0, 21)]


def test_a_run_is_timed_from_its_first_character():
    assert estimate_visemes("ammm", 400) == [(0, 2), (100, 21)]


def test_marks_stay_strictly_increasing_when_the_step_is_under_half_a_millisecond():
    """Neighbouring marks round onto the same millisecond, and speech_marks()
    re-sorts by (t_ms, viseme_id), so an equal-time pair would be resolved by id
    rather than by the order the walk ended on. The later shape wins."""
    marks = estimate_visemes("hello world", 3)
    times = [t_ms for t_ms, _ in marks]
    assert times == sorted(set(times))
    assert marks == [(0, 4), (1, SILENCE_VISEME), (2, 14), (3, 19)]


def test_an_arabic_sentence_produces_real_marks():
    marks = estimate_visemes("مرحبا بك", 800)
    assert marks == [
        (0, 21),
        (100, 13),
        (200, 12),
        (300, 21),
        (400, 2),
        (500, SILENCE_VISEME),
        (600, 21),
        (700, 20),
    ]


def test_a_stretched_arabic_word_is_not_stretched_by_the_tatweel():
    """No normalization is applied, so the tatweel is simply unmapped and the
    two meems collapse into the one shape they are."""
    assert estimate_visemes("مـم", 400) == [(0, 21)]


def test_a_voweled_arabic_word_uses_its_short_vowels():
    assert estimate_visemes("مَ", 200) == [(0, 21), (100, 2)]


def test_whitespace_runs_collapse_before_the_walk():
    assert estimate_visemes("a   b", 100) == estimate_visemes("a b", 100)


def test_the_whitespace_that_collapses_is_the_whitespace_the_other_sdk_collapses():
    """Python's own \\s takes the C1 controls and leaves U+FEFF alone, and the
    other SDK's does the opposite. Read with either shorthand the two SDKs
    count different tokens for one sentence and time every mark differently."""
    assert estimate_visemes("a\ufeffb", 100) == estimate_visemes("a b", 100)
    assert estimate_visemes("a\u0085b", 100) == estimate_visemes("ab", 100)
    assert estimate_visemes("a \u0085", 100) == [(0, 2), (50, SILENCE_VISEME)]


def test_the_marks_reach_the_wire_in_the_order_they_were_estimated():
    """The end of the divergence fix: because the timeline is strictly
    increasing, the builder's sort cannot reorder it."""
    marks = estimate_visemes("hello world", 3)
    sent = json.loads(speech_marks(marks))["marks"]
    assert [(m["tMs"], m["visemeId"]) for m in sent] == marks


# ------------------------------------------------------ real provider timings


def test_real_timings_are_used_as_the_mark_times():
    marks = visemes_from_alignment(["h", "e", "l", "o"], [0.0, 0.25, 0.5, 1.5])
    assert marks == [(0, 12), (250, 4), (500, 14), (1500, 8)]


def test_a_ragged_alignment_is_walked_to_the_shorter_array():
    """Providers do return mismatched lengths, and throwing there would lose the
    turn over a cosmetic hint."""
    assert visemes_from_alignment(["h", "e", "l", "l", "o"], [0.0, 0.1]) == [(0, 12), (100, 4)]
    assert visemes_from_alignment(["h"], [0.0, 0.1, 0.2]) == [(0, 12)]
    assert visemes_from_alignment([], []) == []


def test_a_leading_silence_still_earns_a_mark():
    """The run collapser starts at a sentinel no character can carry. Starting
    it at 0 would swallow this mark, and it is what anchors the mouth shut
    before the first vowel."""
    assert visemes_from_alignment([" ", "a"], [0.0, 0.1]) == [(0, SILENCE_VISEME), (100, 2)]


def test_an_alignment_with_no_mouth_shape_in_it_returns_nothing():
    """Empty is the signal to fall back to the estimator, which is why a
    punctuation-only alignment has to produce it rather than a lone silence."""
    assert visemes_from_alignment([".", " ", "!"], [0.0, 0.1, 0.2]) == []


def test_a_negative_start_time_is_clamped_to_zero():
    assert visemes_from_alignment(["a"], [-0.5]) == [(0, 2)]


def test_a_timing_that_is_not_a_finite_number_costs_only_its_own_mark():
    """A bare NaN in a provider payload parses to a float, and losing the turn
    over one bad number would be the cosmetic hint killing the call."""
    assert visemes_from_alignment(["a", "b"], [float("nan"), 0.5]) == [(500, 21)]
    assert visemes_from_alignment(["a", "b"], [0.0, float("inf")]) == [(0, 2)]


def test_a_run_in_an_alignment_collapses_to_its_first_timing():
    assert visemes_from_alignment(["m", "m", "m"], [0.0, 0.1, 0.2]) == [(0, 21)]


def test_an_unmapped_character_in_an_alignment_is_skipped_not_silenced():
    assert visemes_from_alignment(["a", "3", "b"], [0.0, 0.1, 0.2]) == [(0, 2), (200, 21)]


# ------------------------------------------------------------ the turn's clock


def test_the_turn_counter_starts_at_zero():
    assert TurnLipSync().duration_ms == 0


def test_audio_sent_is_counted_as_the_time_it_plays_for():
    lipsync = TurnLipSync()
    lipsync.audio_sent(ONE_SECOND)
    lipsync.audio_sent(ONE_SECOND[: len(ONE_SECOND) // 2])
    assert lipsync.duration_ms == 1500


def test_the_timeline_is_spread_over_the_audio_actually_sent():
    lipsync = TurnLipSync()
    lipsync.audio_sent(ONE_SECOND)
    assert lipsync.finish("hello") == estimate_visemes("hello", 1000)


def test_finishing_a_turn_resets_the_counter():
    lipsync = TurnLipSync()
    lipsync.audio_sent(ONE_SECOND)
    lipsync.finish("hello")
    assert lipsync.duration_ms == 0


def test_a_turn_that_sent_no_audio_produces_no_marks():
    assert TurnLipSync().finish("hello") == []


def test_a_turn_with_no_text_produces_no_marks_and_still_resets():
    lipsync = TurnLipSync()
    lipsync.audio_sent(ONE_SECOND)
    assert lipsync.finish("") == []
    assert lipsync.duration_ms == 0


def test_a_barge_in_does_not_lengthen_the_next_turn():
    """On cancel the service drops audio the caller never heard. A counter that
    kept those milliseconds would spread the next turn's text over its own audio
    plus the discarded audio, and the mouth would run long from there on."""
    lipsync = TurnLipSync()
    lipsync.audio_sent(ONE_SECOND)
    lipsync.cancel()
    assert lipsync.duration_ms == 0
    lipsync.audio_sent(ONE_SECOND)
    assert lipsync.finish("hello") == estimate_visemes("hello", 1000)


def test_a_duration_can_be_handed_over_directly():
    lipsync = TurnLipSync()
    lipsync.audio_sent_ms(320)
    lipsync.audio_sent_ms(0)
    lipsync.audio_sent_ms(-40)
    assert lipsync.duration_ms == 320


def test_a_chunk_that_measures_as_no_number_does_not_take_the_turn_with_it():
    lipsync = TurnLipSync()
    lipsync.audio_sent_ms(float("nan"))
    lipsync.audio_sent_ms(float("inf"))
    lipsync.audio_sent_ms(200)
    assert lipsync.duration_ms == 200


def test_a_sink_at_another_rate_is_counted_at_that_rate():
    lipsync = TurnLipSync(sample_rate_hz=24_000)
    lipsync.audio_sent(b"\x00\x00" * 24_000)
    assert lipsync.duration_ms == 1000


# ------------------------------------------------------------- the emotion


def test_a_plain_sentence_wears_a_neutral_face():
    assert infer_emotion("the meeting is at four") == "neutral"


def test_blank_text_is_neutral():
    assert infer_emotion("") == "neutral"
    assert infer_emotion("   ") == "neutral"


def test_doubled_punctuation_reads_as_surprise():
    """Checked on the raw text, because punctuation is how a model writes a
    startled reply when none of the surprise words appear in it."""
    assert infer_emotion("They did what??") == "surprised"
    assert infer_emotion("Really!!") == "surprised"


def test_a_single_question_mark_is_not_surprise():
    assert infer_emotion("Shall we start?") == "neutral"


def test_surprise_outranks_an_apology_in_the_same_sentence():
    """Priority, not scoring: a startled 'wow' must not be averaged away."""
    assert infer_emotion("Wow, sorry, that is great") == "surprised"


def test_an_apology_outranks_an_incidental_good_word():
    assert infer_emotion("Sorry, that is great news") == "sad"


def test_a_happy_word_is_happy():
    assert infer_emotion("Congratulations, that is excellent") == "happy"


def test_a_word_boundary_stops_a_partial_match():
    """Without the bounds 'nicety' and 'greatly' put a smile on the tile for
    words nobody stressed."""
    assert infer_emotion("a nicety of greatly increased scope") == "neutral"


def test_a_smart_apostrophe_still_reads_as_an_apology():
    """Models emit the typographic apostrophe constantly, and this is the single
    most common apologetic phrasing there is."""
    assert infer_emotion("I can’t do that") == "sad"
    assert infer_emotion("I’m unable to reach it") == "sad"
    assert infer_emotion("I can't do that") == "sad"


def test_an_english_word_running_into_an_arabic_one_still_reads_as_english():
    """The bounds are ASCII on purpose. Python's Unicode \\b finds no boundary
    between an Arabic letter and an English one, so this sentence would infer
    neutral here and happy in the other SDK, off the same lexicon."""
    assert infer_emotion("سlove") == "happy"
    assert infer_emotion("greaté") == "happy"


def test_an_arabic_reply_infers_neutral_by_design():
    """The lexicon is English only. Neutral is the safe face, and a guess at the
    language would put a confident wrong one on the tile."""
    assert infer_emotion("شكرا جزيلا، هذا رائع") == "neutral"


def test_every_inferred_emotion_is_one_the_avatar_knows():
    inferred = {
        infer_emotion(text)
        for text in ("wow!!", "sorry about that", "great work", "the report is ready")
    }
    assert inferred <= set(EMOTIONS)


def test_the_same_emotion_is_not_sent_twice():
    """A partial-per-word stream would otherwise send dozens of identical
    messages for one sentence."""
    cues = ExpressionCue()
    assert cues.cue("great") == "happy"
    assert cues.cue("great work") is None


def test_a_reading_that_shifts_mid_reply_re_cues():
    """Re-inferring on every chunk is what lets the face self-correct as the
    rest of the sentence arrives."""
    cues = ExpressionCue()
    assert cues.cue("let me check that") == "neutral"
    assert cues.cue("let me check that, no way!!") == "surprised"


def test_a_transcript_arriving_mid_tool_does_not_overwrite_the_thinking_face():
    """Without the suppression the caller sees the avatar look finished while it
    is still working."""
    cues = ExpressionCue()
    assert cues.thinking(True) == "thinking"
    assert cues.cue("great") is None
    assert cues.last_sent == "thinking"


def test_a_finished_tool_puts_the_face_back_to_neutral():
    """The model may say nothing at all after a tool result, and with no
    transcript to re-infer from the face would stick mid-thought."""
    cues = ExpressionCue()
    cues.thinking(True)
    assert cues.thinking(False) == "neutral"


def test_the_face_cues_again_once_the_tool_is_done():
    cues = ExpressionCue()
    cues.cue("great")
    cues.thinking(True)
    cues.thinking(False)
    assert cues.cue("great work") == "happy"


def test_setting_the_same_thinking_state_twice_is_a_no_op():
    cues = ExpressionCue()
    assert cues.thinking(True) == "thinking"
    assert cues.thinking(True) is None


def test_leaving_a_thinking_state_that_was_never_entered_sends_nothing():
    cues = ExpressionCue()
    assert cues.thinking(False) is None


def test_the_neutral_reset_fires_once_and_not_again():
    """Only a transition acts, so a second finally, or a retry around the tool,
    sends nothing."""
    cues = ExpressionCue()
    cues.thinking(True)
    assert cues.thinking(False) == "neutral"
    assert cues.thinking(False) is None
