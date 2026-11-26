"""Normalize legacy dictionary symbols only within pronunciation fields."""


def normalize(value):
    # Some ECDICT records encode the voiced velar stop as ASCII caret.
    # Use the IPA single-storey g, preserving every other pronunciation symbol.
    return value.replace('^', '\u0261') if isinstance(value, str) else value


def normalize_fields(value):
    """Return corrected pronunciation fields without changing prose or formulas."""
    if isinstance(value, dict):
        return {key: normalize(item) if key == 'phonetic' else normalize_fields(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_fields(item) for item in value]
    return value
