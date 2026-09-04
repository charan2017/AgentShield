from typing import Dict, Any


def calculate_risk(
    amount: float,
    category: str,
    merchant_known: bool = True,
    unusual: bool = False,
) -> Dict[str, Any]:

    score = 0
    reasons = []

    # Amount risk
    if amount >= 10000:
        score += 35
        reasons.append("High transaction amount")

    elif amount >= 5000:
        score += 20
        reasons.append("Elevated transaction amount")

    else:
        score += 5

    # Merchant risk
    if not merchant_known:
        score += 25
        reasons.append("Unknown merchant")

    # Behavioral anomaly
    if unusual:
        score += 30
        reasons.append("Unusual spending pattern")

    # High-risk categories
    risky_categories = {
        "gambling",
        "crypto",
        "cryptocurrency",
    }

    if category.lower() in risky_categories:
        score += 35
        reasons.append("High-risk payment category")

    score = min(score, 100)

    if score >= 70:
        level = "HIGH"

    elif score >= 40:
        level = "MEDIUM"

    else:
        level = "LOW"

    return {
        "risk_score": score,
        "risk_level": level,
        "risk_reasons": reasons,
    }