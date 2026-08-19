"""The shared percentile, and the specific way it was once wrong.

This function had three separate implementations before it had one, and the odd
one out reported p50 of a four-sample set as its third value. Nothing caught it
because nothing tested it -- a percentile helper looks too small to be worth a
test right up until two of them disagree.
"""

import pytest

from app.vault.measurement import percentile


def test_p50_is_not_the_upper_half() -> None:
    """The regression that motivated consolidating this.

    An earlier copy indexed with ``int(len(values) * fraction)``, which put p50
    of a four-sample set at index 2 -- the third value, and roughly a p75. The
    number was plausible, monotonic, and wrong, which is why it survived.
    """

    assert percentile([10.0, 20.0, 90.0, 100.0], 0.5) == 20.0


def test_p50_of_two_samples_is_the_lower_one() -> None:
    """Nearest rank of ceil(0.5 * 2) = 1, so the first value.

    The same earlier copy returned the maximum here, which reads as a plausible
    median on a two-sample set and is the hardest case to notice by eye.
    """

    assert percentile([10.0, 90.0], 0.5) == 10.0


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [
        (0.0, 1.0),
        (0.5, 3.0),
        (0.95, 5.0),
        (0.99, 5.0),
        (1.0, 5.0),
    ],
)
def test_returns_an_observed_value_at_every_fraction(
    fraction: float,
    expected: float,
) -> None:
    """Never interpolated: every answer is a value that was actually measured."""

    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], fraction) == expected


def test_ignores_input_ordering() -> None:
    ordered = percentile([1.0, 2.0, 3.0, 4.0], 0.75)
    shuffled = percentile([3.0, 1.0, 4.0, 2.0], 0.75)

    assert ordered == shuffled == 3.0


def test_single_sample_is_its_own_every_percentile() -> None:
    for fraction in (0.0, 0.5, 0.99, 1.0):
        assert percentile([7.0], fraction) == 7.0


def test_empty_sample_raises_rather_than_reporting_zero() -> None:
    """A fabricated 0.0 is indistinguishable from a real measurement.

    Reported as a latency it would flatter whatever it summarised, so the caller
    has to decide what "no observations" means rather than being handed a number
    that looks like one.
    """

    with pytest.raises(ValueError, match="empty sample"):
        percentile([], 0.5)
