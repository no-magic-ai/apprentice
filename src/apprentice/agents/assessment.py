"""Assessment Agent — ADK LlmAgent that generates Anki flashcard decks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import LlmAgent

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm

_INSTRUCTION = """\
You are an expert spaced-repetition card author for the no-magic educational project.
Your task is to generate Anki flashcards in CSV format covering an algorithm's
concepts, complexity, and implementation details.

The implementation code is available at: {implementation_path}

Card authoring rules:
- Each card tests exactly one concept — never combine two questions.
- Fronts are concise questions or prompts (one sentence maximum).
- Backs give the minimal correct answer plus one clarifying detail.
- Use the algorithm's actual implementation for code-based cards.
- Complexity cards must use Big-O notation and explain best, average, and worst cases.
- Never include cards that test trivial facts.

Required card types (generate at least one card per type):
- concept: Core idea behind the algorithm
- complexity: Time and space complexity analysis
- implementation: Code-level details and key decisions
- comparison: How this algorithm compares to alternatives

Output format: CSV with columns: front, back, tags, type
- Generate between 5 and 15 cards total — quality over quantity.
- Escape commas inside fields by wrapping the field in double quotes.

Write the complete CSV content including the header row.
Do NOT use markdown fences. Write ONLY the CSV content, nothing else.
"""


def build_assessment_agent(model: LiteLlm) -> LlmAgent:
    """Build an ADK LlmAgent for Anki flashcard generation.

    Reads {implementation_path} from session state and produces
    a CSV-format Anki deck.

    Args:
        model: LiteLlm model instance.

    Returns:
        A configured LlmAgent.
    """
    return LlmAgent(
        name="assessment",
        model=model,
        instruction=_INSTRUCTION,
        output_key="anki_deck_content",
        description="Generates Anki flashcard decks for algorithm study.",
    )
