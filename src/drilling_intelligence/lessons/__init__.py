"""What a field learns, and who is allowed to say it is true.

Lessons, best practices and recommendations are one pipeline with three standards of proof, which is why
they share a package and not a table:

*   a **lesson** is a claim about what happened - cheap to write, expensive to approve, because approval
    is what makes it quotable; it requires evidence and a reviewer who is not the author;
*   a **best practice** is a lesson that survived more than one well, promoted out of an approved lesson
    and carrying its provenance, with its own approval and its own exceptions;
*   a **recommendation** is advice the platform derived from records.  It is a proposal with its evidence
    and its query stored beside it, and only a person can move it out of ``PROPOSED``.

The rule that holds all three up: nothing here is approved by a script.  A language model may one day
draft a lesson or propose a practice, and the platform's answer to that is the same as its answer to a
promoted table row - it arrives as a candidate, with provenance, and waits for someone to confirm it.
"""

from .repository import LessonRepository

__all__ = ["LessonRepository"]
