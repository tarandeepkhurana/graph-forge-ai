"""Greetings must not touch the document.

"hi what's up" used to run entity matching, graph traversal and vector search,
then answer "Hi! I'm here to help" with [1] and [2] attached. The passages had
nothing to do with the greeting -- a nearest-neighbour search returns its
nearest neighbours whatever you give it.

The cost is not the wasted work. A citation has to mean "this came from your
document", and once one appears under a greeting it means nothing anywhere.

The guard is deliberately lopsided: a message must be short AND consist only of
greeting and filler words. Anything else goes to retrieval, so the worst case
is running a search on a greeting, never refusing a real question.
"""

import pytest

from graphforge.ai.chat import is_small_talk


@pytest.mark.parametrize("message", [
    "hi", "Hi!", "hello", "hello there", "hey man", "yo", "howdy",
    "hi what's up", "whats up", "what's up?", "how are you", "how's it going",
    "good morning", "thanks", "thanks!", "thank you", "ty", "ok", "ok cool",
    "great", "bye", "goodbye",
])
def test_greetings_skip_retrieval(message):
    assert is_small_talk(message) is True


@pytest.mark.parametrize("message", [
    # The subject is what matters, not the greeting wrapped around it.
    "whats up with batch learning",
    "hi, how does online learning work?",
    "what's online learning use cases",
    "what is batch ml",
    "how does partial_fit work",
    "thanks that helps",
    "explain it",
    "difference between online and incremental",
])
def test_real_questions_still_retrieve(message):
    assert is_small_talk(message) is False


def test_long_messages_are_never_small_talk():
    # A guard on a short message is safe; on a long one it would start
    # swallowing questions that merely open politely.
    assert is_small_talk("hi " * 20) is False


def test_empty_message_is_not_small_talk():
    # Handled earlier as an error; it must not fall into the greeting path.
    assert is_small_talk("") is False
    assert is_small_talk("   ") is False
