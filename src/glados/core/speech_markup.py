"""Small streaming protocol for speech with optional avatar directions."""

from dataclasses import dataclass

EMOTIONS = frozenset(
    {
        "neutral",
        "quizzical",
        "angry glare",
        "suspicious",
        "smug",
        "surprised",
        "bored",
        "disappointed",
        "processing",
        "offline",
    }
)
SPEECH_DIRECTION_PROMPT = """When replying aloud, you may direct your avatar with
[emotion:NAME] before the words to speak. Available expressions: neutral,
quizzical, angry glare, suspicious, smug, surprised, bored, disappointed.
The expression applies to following speech until the next marker, and resets
for each response. Use a few deliberate changes, preferably between sentences.
Example: [emotion:smug]Excellent work. [emotion:disappointed]For a human.
These markers are silent stage directions, not words to say. Write normal
speakable text around them. Do not put these markers in tool arguments or code.
"""


@dataclass(frozen=True)
class SpeechText:
    text: str
    emotion: str | None = None
    generation: int | None = None
    autonomy_generation: int | None = None
    autonomy_cycle: str | None = None


class SpeechMarkupParser:
    """Accept arbitrarily split chunks; never pass control markers to TTS.

    Known emotion markers ([emotion:NAME]) are extracted and applied to
    following text. Unknown bracket-only content (e.g. [health], [emotion]
    without a colon) and angle-bracket content (</system>, <reasoning>,
    JSON fragments) are silently discarded so they never reach TTS.
    Unfinished brackets at EOS are dropped to avoid leaking partial markers.
    """

    PREFIX = "[emotion:"

    def __init__(self) -> None:
        self.emotion: str | None = None
        self._pending = ""
        self._marker: str | None = None
        # Track discard modes with truthy values; empty string means off.
        self._bracket_pending: str = ""
        self._angle_pending: str = ""

    def feed(self, chunk: str, *, final: bool = False) -> list[SpeechText]:
        segments: list[SpeechText] = []
        text = ""
        for char in chunk:
            # Inside unknown bracket [...]: discard until ]
            if self._bracket_pending:
                if char == "]":
                    self._bracket_pending = ""
                    continue
                self._bracket_pending += char
                continue
            # Inside angle bracket <...>: discard until >
            if self._angle_pending:
                if char == ">":
                    self._angle_pending = ""
                    continue
                self._angle_pending += char
                continue
            # Inside known emotion marker [emotion:NAME]: extract name
            if self._marker is not None:
                if char == "]":
                    name = self._marker.strip().lower()
                    if name in EMOTIONS:
                        self.emotion = name
                    self._marker = None
                elif len(self._marker) < 64:
                    self._marker += char
                continue
            self._pending += char
            # BEFORE stripping: check if we have an unknown bracket/angle
            if not self._bracket_pending and not self._angle_pending:
                if self._pending.startswith("[") and not self.PREFIX.startswith(self._pending):
                    # Starts with [ but NOT [emotion: -> unknown bracket
                    if char == "]" and self._pending.endswith("]"):
                        # Bracket already closed: discard bracket content,
                        # don't enter discard mode (subsequent chars are normal).
                        self._pending = ""
                        continue
                    self._bracket_pending = "1"
                    self._pending = ""
                    continue
                if self._pending.startswith("<"):
                    # Angle bracket -> discard
                    self._angle_pending = "1"
                    self._pending = ""
                    continue
            while self._pending and not self.PREFIX.startswith(self._pending):
                text += self._pending[0]
                self._pending = self._pending[1:]
            if self._pending == self.PREFIX:
                if text:
                    segments.append(SpeechText(text, self.emotion))
                    text = ""
                self._pending = ""
                self._marker = ""
            # After while loop: check if we need to detect an unknown bracket
            # This handles the case where _pending was "[" followed by a character
            # that broke the emotion prefix, and the while loop stripped everything.
            # We can't recover the "[" at this point, but the pre-strip check
            # above catches it on the FIRST non-matching character.
        if final:
            if self._pending and not self._pending.startswith("[e"):
                text += self._pending
            self._pending = ""
            self._marker = None
            self._bracket_pending = ""
            self._angle_pending = ""
        if text:
            segments.append(SpeechText(text, self.emotion))
        return segments
