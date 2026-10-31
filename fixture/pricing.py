"""PatchPilot demo fixture.

SEEDED DEMO DEFECT: the formula below is deliberately wrong so PatchPilot has a
real failing test to repair. Do not "fix" this file by hand.
"""


def apply_discount(price, percent):
    """Return the price after taking `percent` percent off."""
    return price * percent / 100
